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

## メインセッション Agent Identity・定義束縛・Skill 証跡（main_agent_identity / agent_definition / skill_evidence, Issue #2046）

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

### 非mutation hermetic レーン（変更を一切行わない検証経路、`--hermetic-agent-definition`）

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

## `--claude-bin` による launcher 選択（Issue #2174）

`--claude-bin <absolute path>` は `--runtime claude` 限定のオプション入力であり、
claude 互換の実行ファイル（例: `scripts/claude-gpt/launch.sh` のような
claude-gpt launcher）の絶対パスを明示的に指定できる。

- **structured lane**: `preflight_claude_available()` が `shutil.which("claude")`
  による PATH 解決を行わず、`--claude-bin` の絶対パスをそのまま実行ファイルと
  して使用する（存在確認・実行可能性確認は行うが、PATH lookup は一切行わない）。
  以降の version capture・固定 argv 実行はすべてこの絶対パスに対して行われる。
- **interactive herdr lane**: herdr 自体は `--kind claude` の実行ファイルを
  常に自分自身の PATH lookup で解決し、明示的な binary path を受け付ける
  flag を持たない。そのため runner は isolated session 専用の一時ディレクトリを
  作成し、その中に `claude` という名前で `--claude-bin` の絶対パスへの
  symlink を置いた上で、そのディレクトリを isolated session の環境変数
  `PATH` の先頭に追加してから `herdr workspace create` / `herdr agent start`
  を実行する。isolated session 終了時（cleanup の一部として）にこの一時
  ディレクトリを削除する。
- `--claude-bin` 未指定時（既定の `None`）は、structured lane・interactive
  lane のいずれも既存の `shutil.which("claude")` PATH 解決から一切変更されない。

## `--claude-adapter` による launcher 固有 argv/env 制御（Issue #2174 AC1 fix_delta）

OWNER REQUEST_CHANGES（https://github.com/squne121/loop-protocol/issues/2174#issuecomment-5302215173）
により、`--claude-bin` 単体（`bool(--claude-bin)`）を launcher 固有挙動の判定材料に
してはならないと修正された。従来の実装は `--claude-bin` が指定されているという
事実だけで、常に structured lane の argv 先頭へ `--` separator を挿入し、固定
`--settings <hook-observability-json>` を省略して代わりに
`CLAUDE_GPT_RUNTIME_SMOKE_HOOKS=subagent-start-stop` 環境変数を注入していた。
これは「binary path」と「launcher protocol」を混同する設計であり、絶対パス指定の
native claude binary や透過的な wrapper に対しても意図せず claude-gpt 固有の
argv/env 変換が適用されてしまう欠陥だった。

`--claude-adapter native|claude-gpt`（既定 `native`）は、この launcher 固有挙動を
`--claude-bin` から独立させた明示入力である:

- `native`（既定）: `--claude-bin`（指定時）は純粋な binary path override。argv は
  PATH 解決時と byte-identical（固定 `--settings <hook-observability-json>` を維持）。
  `CLAUDE_GPT_RUNTIME_SMOKE_HOOKS` は一切設定しない。
- `claude-gpt`: `--claude-bin` の指定が必須（未指定なら `parser.error` で起動前に
  blocked）。structured lane の固定 argv 先頭に literal `--` separator を挿入し
  （`scripts/claude-gpt/launch.sh` は自身のオプション（`--claude-bin` /
  `--check-only` / `--dry-run`）の後に `--` を要求し、以降を claude 本体へ素通しする）、
  `--settings <JSON>` の代わりに `CLAUDE_GPT_RUNTIME_SMOKE_HOOKS=subagent-start-stop`
  環境変数を子プロセスへ設定する（launcher が `--settings` を含む policy-weakening
  flag を forbidden として拒否するため）。hermetic no-mutation lane
  （`--hermetic-agent-definition`）が同時に有効な場合、hermetic 用の
  `--settings <hermetic_settings_file>` は引き続き argv へ追加されるため、
  `--` の後に launcher が拒否する `--settings` が渡り launcher 自身の
  `unknown_launcher_option`/`policy_weakening_flag_rejected` 拒否で構造的に失敗する
  （Issue #2174 AC8 が要求する検出可能な組み合わせ）。この拒否 receipt は
  `extract_claude_gpt_launcher_receipt()` で stderr の `CLAUDE_GPT_LAUNCH_RESULT_V1`
  JSON 行から抽出され、evidence の `claude_gpt_launcher_receipt` に記録される。

## Herdr 全体 snapshot 保全検証（Issue #2174 AC7）

`--mode interactive` は常に（オプトインではなく）、isolated interactive lane 実行の
前後で呼び出し元自身の（デフォルト、非 isolated）Herdr session の状態を
`capture_herdr_session_snapshot` 相当の 2 endpoint から取得し比較する:

- `herdr session list --json`: 既存の `snapshot_herdr_sessions`
  （`name`/`running`/`default`/`socket_path`/`session_dir`、Issue #2176 P0-3）
- `herdr api snapshot`: 新規 `capture_herdr_workspace_snapshot`
  （`result.snapshot.agents[].workspace_id`/`tab_id`/`pane_id`、および
  `focused_workspace_id`/`focused_tab_id`/`focused_pane_id`）

いずれかのフィールドが取得不能な場合は `capture_herdr_workspace_snapshot` が
`None` を返し、`diff_herdr_workspace_snapshot` はこれを fail-closed で
preservation failure として扱う（部分的な snapshot を証跡として扱わない）。
この run 自身が作成した isolated session は比較対象から除外されるが、それ以外の
workspace/agent/focus の変化は 1 件でも検出されると run 全体が FAIL する。
`scripts/agent-ops/tests/test_run_worktree_agent_runtime_smoke_workspace_snapshot.py`
に、focused workspace/tab/pane・agent location それぞれを意図的に変化させた
negative（poison）test と、CLI 経由の end-to-end poison test（ambient snapshot が
isolated lane 実行中に変化するケース）を追加した。

## 観測できる主な evidence

- structured lane: `type: system/init`、`type: result`、hook lifecycle event の件数を確認できる
- interactive lane: `agent explain` から抽出した detected agent／confidence と observed lifecycle state
  （`summary.md` の allowlist フィールドのみ。raw pane transcript は保存しない）

## SubAgent 実行の hook ID 相関による causal evidence 判定関数（`subagent_causal_evidence_verdict()`、Issue #2183 対応）

`subagent_causal_evidence_verdict(stdout, expected_markers=None)`
（`scripts/agent-ops/run_worktree_agent_runtime_smoke.py`）は、SubAgent が実際に
実行されたことの証跡を、marker 文字列（synthetic fixture でも trivially 満たせる）
のみに依存させず、hook lifecycle event の ID 相関という構造的シグナルで判定する
純粋関数。structured lane の captured stdout（`--include-hook-events` の
stream-json）を入力とし、live 実行を新規に起動しない hermetic pytest で全域を
検証できる（本 Issue の Runtime Verification Applicability: `not_applicable`）。

### 利用方法

呼び出し元は structured/interactive いずれの lane でも、captured された
stdout（または herdr pane text）と、検証対象の `expected_markers`（`--expect-marker`
と同じリスト）を渡すだけでよい:

```python
causal_evidence = subagent_causal_evidence_verdict(out, args.expect_marker)
schema_summary["subagent_causal_evidence"] = causal_evidence
```

戻り値は常に `schema_summary["subagent_causal_evidence"]` へ無条件（フラグの
有無に関わらず）に記録される。これにより、実際に exit_code を昇格させたか
どうかとは独立に、「marker のみの PASS だったのか、hook ID 相関で PASS した
のか」を事後に判別できる。

### `causal_evidence_source` の意味

3 値の enum で、`hook_id_correlated` へ昇格するのは以下を **すべて** 満たした
場合に限る（fail-closed。いずれか一つでも欠落・不一致なら昇格しない）:

1. 同一 `agent_id` を持つ `SubagentStart` と `SubagentStop` の hook lifecycle
   event ペアが観測される
2. `SubagentStop` payload の `agent_transcript_path` が回収でき、かつそのパスが
   指すファイルが実在し・読み取り可能・非空である（`agent_transcript_verified`。
   Issue #2183 AC11 — payload にパス文字列があるだけでは足りない）
3. 相関済み `agent_id` が `Agent` tool_use/tool_result の `tool_use_id` 相関
   （`_claude_agent_tool_invocation_correlated`）で裏付けられている
   （`tool_invocation_id_correlated == True`。Issue #2183 AC3 — この bool は
   フィールドとして計算されるだけでなく、判定そのものに参加する）
4. `expected_markers` を指定した呼び出しでは、その期待マーカーが相関済み
   child 自身の transcript ファイル内容、または `SubagentStop` の
   `last_assistant_message` 相当フィールド由来であることが確認できる
   （`marker_provenance_verified`。Issue #2183 AC11/AC12 — parent 自身の
   最終応答文字列にのみ一致した場合は昇格しない。無関係な SubAgent が
   正常に Start/Stop・transcript・tool 相関まで揃って完了しても、期待
   マーカーが parent 自身の最終応答テキストにしか現れない negative
   control シナリオは、この条件で `no_evidence` に fail-close する）

`marker_only_insufficient` は、hook lifecycle event が一切なく `expected_markers`
の文字列のみが `stdout` にある場合。それ以外（lone `SubagentStart`、
`agent_id` 不一致、transcript 未実在/空、tool 相関なし、marker provenance
未確認など）はすべて `no_evidence` になる。

### lane ごとの既定挙動

- **structured lane**（`run_structured_claude` 経由）: `SubagentStart`/
  `SubagentStop` の stream-json チャンネルが常に構造的に利用可能なため、
  呼び出し側が `--expect-marker` を指定する場合、`causal_evidence_source ==
  hook_id_correlated` は **既定強制**（追加フラグ不要。PR #2220 レビュー
  fix-delta）。`--expect-marker` を指定しない structured lane 実行にこの
  要件を課したい場合は `--require-subagent-causal-evidence`（既定 OFF）を
  明示する。
- **interactive lane**（`run_interactive_herdr_isolated` 経由）: herdr pane
  のテキストレンダリングは `--include-hook-events` の stream-json hook
  payload を構造的にエコーしないため、`causal_evidence_source` は今のところ
  `no_evidence`／`marker_only_insufficient` になる想定である。この lane では
  本要件は引き続き **opt-in**（`--require-subagent-causal-evidence` を明示
  した場合のみ exit を昇格）のままとし、既存の operability / multi-turn /
  session isolation の証拠（Herdr session baseline 比較等）を PASS 条件として
  維持する。interactive lane 向けの hook 出力チャネル整備（herdr 側の構造的
  制約変更）は本 Issue の対象外（別 Issue の scope）。
