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

## claude-gpt launcher の同一 session multi-turn / 複数 SubAgent lifecycle / proxy cleanup（Issue #2219、同一セッション複数ターン・複数サブエージェント生存確認）

`--claude-adapter claude-gpt` の run では、以下 3 種類の追加証明が opt-in フラグ経由で
可能になる。いずれも `scripts/claude-gpt/launch.sh` 自体には手を入れず、launcher が
既に emit している stdout stream-json / stderr `KEY=value` 行を parse するだけである。

### 同一 main session 内で最低 N turn（`--require-min-turns`）

```bash
claude -p --output-format stream-json --include-hook-events \
  --no-session-persistence --max-turns "$MAX_TURNS" --verbose
```

`--max-turns >= 2` は Claude Code 自身の agentic loop に複数 turn（tool 呼び出しの
往復）を駆動させる。各 stream-json イベントは `session_id`/`sessionId` フィールドを
持ち、`extract_claude_stream_session_ids()` は登場順に重複排除した全ての distinct
値を返す。`verify_same_main_session_across_turns(stdout, min_turns)` は
（1）distinct session_id が厳密に 1 個であること、（2）`type: "assistant"` イベント数
（`count_claude_stream_turns()`）が `min_turns` 以上であること、の両方を要求する。
どちらか一方でも欠けると `verified: False`（fail-closed）。

設計判断（fix_delta iteration 1、pr-reviewer REQUEST_CHANGES、PR #2222 で改訂）:
初回実装は選択肢 A のみを採用し、Issue #2219 が第一候補として挙げていた選択肢 B
（interactive herdr lane への `--additional-prompt` 相当フラグの再実装、PR #2176 が
commit `06d8baa9` で prototype し、Issue #2174 のスコープ外として commit `5a44ebf0`
で revert 済み）を試みていなかった。この revert はスコープ判断であり技術的な却下では
ないため、fix_delta iteration 1 で選択肢 B を Issue #2219 の scope で再実装し、
選択肢 A/B の両方を利用可能にした。structured lane は引き続き選択肢 A
（`--max-turns` が駆動する単一 session 内 agentic loop）を使う。interactive lane は
選択肢 B（`--additional-prompt`）を使い、下記「interactive lane の同一 session
multi-turn（`--additional-prompt`、選択肢 B）」で詳細を説明する。

### 【superseded】interactive lane の同一 session multi-turn の evidence source（Option 1 へ移行）

**この節（`--additional-prompt` 自体は現行のまま有効。以下の transcript-based
evidence source の記述は historical であり、現行の PASS authority ではない）**:
OWNER anchor 決定（Issue #2219 本文、2026-08-16）により、interactive lane の
multi-turn/multi-SubAgent evidence source は、下記の transcript-existence
ベース設計（Option B）から、**hook-event evidence channel（Option 1）**へ
置き換えられた。理由: live 検証（PR #2222 fix_delta iteration 2）で、
herdr-PTY-driven な claude-gpt session が構造的にフラットな main transcript
（`<session-id>.jsonl`）を一切書かないことが再現されたため（書かれるのは
`subagents/agent-*.meta.json` という completion field を持たない fragmentary
metadata のみ）。下記の transcript-based 記述はこの falsify された設計判断の
記録として保持する。現行の hook-event evidence channel の詳細は
`### interactive lane の hook-event evidence channel（フック事象証拠経路、選択肢 1、現行方式）` を参照。

### interactive lane の同一 session multi-turn（`--additional-prompt`、選択肢 B）

`run_interactive_herdr_isolated()` は既に起動済みの、単一の herdr
`session_name`/`agent_name`（関数内のローカル変数として一度だけ生成され、
この関数全体のライフサイクルを通して同じ値のまま使われる）に対して、初回
`--prompt-file` の turn に続けて `--additional-prompt`（repeatable）で指定した
追加 turn を、既存の `_send_prompt_turn()`（stall-recovery 込みの send/wait
helper。初回 turn と完全に同じロジック）で順次送信する。`evidence["turns_completed"]`
は実際に settle した turn 数（初回 + 追加）を記録する。

interactive lane には structured lane の `--output-format stream-json` に相当する
native stdout イベントストリームが存在しない（TUI の pane transcript は plain text
であり、hook イベントの JSON 形状を確実に判別できない）。しかし interactive lane は
`--no-session-persistence` を forward しない（structured-only flag、AC5）ため、
Claude Code は通常どおりこの run 自身の session transcript を
`~/.claude/projects/<cwd-slug>/<session_id>.jsonl` へ永続化する。この transcript は
structured lane の stdout と同じ stream-json イベント形状（`session_id`/`sessionId`、
`type: "assistant"`、`tool_use_result.agentId`/`agentType`）を持つ。

`_find_claude_interactive_transcript(worktree, since_epoch, claude_adapter)` は
`_resolve_claude_projects_root(claude_adapter)` が解決した projects root 配下の
`*/*.jsonl` を走査し、（1）mtime が `since_epoch`（run 開始直前に記録）以降であること、
（2）先頭から最大 `_TRANSCRIPT_CWD_SCAN_LINES`（50）行以内に現れる `cwd` フィールドが
`worktree` と厳密一致することの両方を満たすファイルを content-linked に特定する
（filename からの推測ではない）。一致するファイルが複数ある場合は最も新しい mtime の
ものを採用し、1 件も見つからない場合は `None`（`interactive_transcript_found: False`）
を返す -- 推測しない。

**Issue #2219 fix_delta iteration 2（claude-gpt adapter に対する live 再検証で発見した
2 つの実バグの修正）**:

1. **projects root のハードコード**: 旧実装は `~/.claude/projects` を常に固定で
   走査していたため、`--claude-adapter claude-gpt` の run では常に
   `interactive_transcript_found: False` になっていた（`scripts/claude-gpt/launch.sh`
   は自身の Claude Code config root を `$CLAUDE_GPT_HOME/claude`（既定
   `~/.claude-gpt/claude`、`scripts/claude-gpt/lib.sh` の
   `claude_gpt_claude_config_dir` と同じ既定式）に隔離し、`CLAUDE_CONFIG_DIR` として
   export するため、実際の session transcript は
   `$CLAUDE_GPT_HOME/claude/projects/<cwd-slug>/<session-id>.jsonl` に永続化される）。
   `_resolve_claude_projects_root()` を新設し、`claude_adapter` に応じて正しい root
   を返すよう修正した。live filesystem 調査（`~/.claude-gpt/claude/projects/.../<session-id>.jsonl`
   の直接確認）の結果、claude-gpt adapter は native adapter と **全く同じ flat
   single-file の stream-json 形状** で transcript を書き出しており、adapter 固有の
   transcript 形状の作り分けは不要と判明した（root だけが異なる）。
2. **`cwd` フィールドの位置の誤仮定**: 旧実装は transcript の **先頭行のみ**
   `cwd` フィールドを確認していたが、live transcript（native / claude-gpt 双方）を
   実地確認した結果、先頭行は `{"type": "mode", ...}` のような session
   bookkeeping レコードで `cwd` を持たず、`cwd` は最初の実メッセージレコード
   （典型的には 3〜4 行目）に初めて現れることが判明した。そのため旧実装は
   claude-gpt に限らず **native adapter でも** transcript を実質的に発見できて
   いなかった（先頭行只の check が常に不一致になるため）。`_TRANSCRIPT_CWD_SCAN_LINES`
   行分の window で `cwd` を探すよう修正した。

この transcript のテキストを、structured lane と全く同じ
`classify_claude_multi_child_lifecycle()` / `verify_same_main_session_across_turns()`
/ `verify_no_forbidden_marker()` へそのまま渡す。Option A 用に実装したこれらの関数を
Option B の共有 building block として再利用しており、別ロジックを再発明していない。
transcript が見つからない場合は空文字列を渡すため、`--require-min-turns` /
`--require-min-subagents` が指定されていれば fail-closed に FAIL する。

`--require-min-turns` を interactive mode で指定する場合、`--max-turns`（structured
lane 専用で interactive lane には forward されない）ではなく、`1 +
len(--additional-prompt)` がその値以上であることが起動前に `parser.error` で
validate される。

### interactive lane の hook-event evidence channel（フック事象証拠経路、選択肢 1、現行方式）

`--require-min-turns`/`--require-min-subagents` のいずれかが指定された
`--runtime claude --mode interactive` run では、`run_interactive_herdr_isolated()`
が `hook_sink_enabled=True` で呼ばれ、以下を行う:

1. `uuid.uuid4().hex` で `hook_sink_nonce` を生成する（この run 専用、caller
   から渡されない）。
2. **claude-gpt adapter**: `claude_gpt_hook_sink_path(nonce)`
   （= `claude_gpt_proxy_state_dir_python() / f"hook-sink-{nonce}.jsonl"`、
   `scripts/claude-gpt/lib.sh` の `claude_gpt_proxy_state_dir()` の Python
   ミラー）でパスを決定し、`CLAUDE_GPT_RUNTIME_SMOKE_HOOKS=hook-sink-multi-turn`
   と `CLAUDE_GPT_HOOK_SINK_NONCE=<nonce>` を `herdr workspace create --env`
   （かつ pane 再 pin の `export`）で isolated session に注入する。
   `scripts/claude-gpt/launch.sh` 自身がこの2つの env var からパスを
   再構築し、settings.json の `hooks`/`env` ブロックへ書き込む。
3. **native adapter**: `tempfile.mkdtemp()` で harness 専用ディレクトリを作り、
   `hook_sink_writer.py`（`_HOOK_SINK_WRITER_SOURCE` 定数）と、5 hook すべてを
   `python3 "<writer>"` にバインドした `settings.json` をそこに書き、
   `CLAUDE_CONFIG_DIR=<そのディレクトリ>/claude-config` を同じ `--env`/
   pane 再 pin パターンで注入する（`scripts/claude-gpt/**` は一切触らない）。
4. run 終了直前（`finally` で一時ディレクトリを消す前）に
   `parse_claude_gpt_hook_sink_records(sink_path)` で sink を読み、
   `evidence["hook_sink_records"]` / `evidence["hook_sink_malformed_line_count"]`
   に格納する。

`main()` 側では、hook sink records を以下の関数へ渡す（structured lane と
**同一のアルゴリズム核** `_pair_agent_lifecycle()` を経由する）:

- `verify_claude_gpt_hook_sink_not_stale(records, expected_nonce)`: 3-way
  `run_nonce` 一致検証（`verify_evidence_not_stale()` とは独立）。
- `classify_claude_hook_sink_multi_child_lifecycle(records, min_required)`:
  `SubagentStart`/`SubagentStop` の agent_id を `_pair_agent_lifecycle()` へ
  渡す（structured lane の `classify_claude_multi_child_lifecycle()` と
  同じ core）。
- `verify_claude_gpt_hook_sink_multi_turn(records, min_turns)`:
  `UserPromptSubmit`/`Stop`/`StopFailure` の `session_id` から同一
  session 内の paired turn 数を判定する。
- `verify_claude_gpt_hook_sink_no_raw_content(records)`: `prompt_digest` が
  64-hex-char の sha256 digest 以外を含んでいないかを構造的に検証する
  （AC13）。

`transcript_path`/`interactive_transcript_subagent_only_session_dirs` は
`summary.md` に advisory フィールドとして残り続けるが、`multi_child_lifecycle`
/`same_session_across_turns` の `verified` 判定には使われない
（`schema_summary["multi_child_lifecycle_source"] == "hook_event_sink"` /
`schema_summary["same_session_across_turns_source"] == "hook_event_sink"`
がこれを示す）。

### 複数 SubAgent の spawn/completion 証明（`--require-min-subagents`）

既存の `extract_claude_hook_lifecycle_events()`（`SubagentStart`/`SubagentStop`
hook lifecycle）と `tool_use_result.agentId`（synchronous completion）の 2 チャネルを
そのまま再利用し、`classify_claude_multi_child_lifecycle(stdout, min_required)` が
agent_id ごとに spawn/stop を集計する:

- `spawned_agent_ids` ⊇ `completed_agent_ids` の共通部分（`paired_agent_ids`）が
  `min_required` 以上
- `orphan_starts`（spawn はあったが completion がない agent_id）が空
- `unknown_children`（completion はあったが spawn がない agent_id。agent_id mismatch
  や偽装された completion を含む）が空
- `duplicate_completions`（同一 agent_id の completion が複数回観測された）が空

いずれか 1 つでも該当すれば `verified: False` となり run は FAIL する。

**Issue #2219 fix_delta iteration 2（async completion チャネルの追加）**: live
claude-gpt session の実地調査で、ASYNC dispatch された SubAgent の
`tool_use_result` は `status: "async_launched"`（`SPAWN_LAUNCH_MODE_ASYNC`）の
まま **その場では `"completed"` に遷移しない** ことが判明した。実際の completion
通知は、後続の別レコード（`type: "queue-operation"` / `"queued_command"` の
`content`/`prompt` 文字列に埋め込まれた `<task-notification>` block）として
transcript 内に到着する:

```
<task-notification>
<task-id>a2be4b1bac93c3190</task-id>
...
<status>completed</status>
...
</task-notification>
```

（これは Claude Code 自身の async Task dispatch 通知プロトコルであり、claude-gpt
固有の挙動ではない。structured lane の単一 child 向け実装（Issue #2015 AC11）が
`CHILD_TERMINAL_STATUS_ASYNC_NO_STOP` として completion 未確認のまま残していた
既知のギャップと同じ根本現象。interactive lane は run 全体の持続時間分だけ長く
transcript を観測できるため、後続の task-notification が到着する時間的余裕がある。）

`extract_claude_task_notification_completions(text)` は raw transcript テキストを
直接正規表現でスキャンし（通知 payload は JSON フィールドとしてではなく、JSON
文字列値の中にエスケープされたテキストとして埋め込まれているため）、
`<status>completed</status>` を報告している `<task-id>` を集合として返す。
`classify_claude_multi_child_lifecycle()` はこれを **既に spawn 済みと確認できた
agent_id にのみ** completion として bind する（spawn 観測なしに completion 通知
単独から spawn を捏造しない）。

### 独立 proxy cleanup 再確認（launcher の全 run で自動）

`launch.sh` は起動直後に stderr へ次を emit する:

```
CLAUDE_GPT_PROXY_PORT=<port>
CLAUDE_GPT_PROXY_LOG=<log path>
CLAUDE_GPT_PROXY_PID=<pid>
```

終了直前には次を emit する:

```
CLAUDE_GPT_PROXY_CLEANUP_OK=<true|false>
CLAUDE_GPT_CLAUDE_EXIT_CODE=<exit code>
```

`extract_claude_gpt_proxy_sidechannel(stderr)` はこれらを parse するのみで、
`launch.sh` 自身のクリーンアップ判定ロジックを再実装しない。
`verify_claude_gpt_proxy_cleanup_independent(proxy_pid, proxy_port)` は
`CLAUDE_GPT_PROXY_CLEANUP_OK` の自己申告を信用せず、`kill(pid, 0)`（プロセス生存確認）
と `ss -ltn`（listen socket 確認）を **この runner プロセス自身が** bounded
poll-with-retry（既定 3 回、0.5 秒間隔）で再実行する。自己申告が `true` でも
独立再確認が clean を確認できなければ run 全体を FAIL とする（launcher の自己申告を
権威にしない、Issue #2219 AC7）。

### claude-gpt adapter のセッションデータの実際の形状（Issue #2219 fix_delta iteration 2 live 調査）

`$CLAUDE_GPT_HOME/claude/projects/<cwd-slug>/<session-id>/subagents/` 配下には
spawn された SubAgent ごとに `agent-<agent-id>.meta.json`
（`{"agentType", "description", "toolUseId", "spawnDepth", "parentAgentId"?,
"worktreeCleanlyRemoved"?}`）が書かれる。**これは spawn 時のメタデータのみであり、
completion の有無を示すフィールドを一切含まない**（`worktreeCleanlyRemoved` は
その SubAgent 自身が worktree を持っていた場合にのみ現れる、cleanup 状況の傍証で
あって completion 信号ではない）。同ディレクトリには一部の SubAgent についてのみ
`agent-<agent-id>.jsonl`（その SubAgent 自身の完全な transcript）も存在するが、
全 SubAgent に対して一律には書かれない。

一方、`<cwd-slug>/<session-id>.jsonl`（session ディレクトリと同じ階層にある、
親セッション自身の flat transcript）には、上記の spawn メタデータと **同じ
agent_id を用いた** spawn evidence（`tool_use_result.agentId`）と completion
evidence（`<task-notification>` block、上記参照）の両方が揃っている。

したがって本 harness は `subagents/*.meta.json` を evidence source として
使わない（completion 判定に使えるフィールドを持たないため）。親セッションの
flat `.jsonl` transcript 1 つだけで spawn + completion の両方を証明できる、
という結論に至った（native adapter と同型の evidence source を、正しい root
の下で読むだけでよい）。

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

### `causal_evidence_source` の意味（PR #2220 OWNER REQUEST_CHANGES
P0-2/P0-3/P1-1/P1-2 で強化、
https://github.com/squne121/loop-protocol/pull/2220#issuecomment-5309790514）

3 値の enum で、`hook_id_correlated` へ昇格するのは、単一の曖昧さのない
Start/Stop chain が以下を **すべて** 満たした場合に限る（fail-closed。
いずれか一つでも欠落・不一致・曖昧なら昇格しない）:

1. 同一 `agent_id` を持つ `SubagentStart` と `SubagentStop` の hook lifecycle
   event ペアが観測され、かつ Start 自身の `stream_index`（captured stdout
   内の出現順序）が Stop より構造的に先行している（P0-3 — 逆順や壊れた
   ストリームで Stop が先に現れるケースを弾く）
2. 両イベントが `session_id` を持つ場合は同一であること、`prompt_id` を
   持つ場合は同一であること、`agent_type` を持つ場合は同一であること
   （いずれも P0-3。値を持たない場合はその条件はスキップされる — 「取得
   できる場合」の best-effort 契約）
3. 同一 `agent_id` に紐づく `SubagentStart`／`SubagentStop` がそれぞれ
   ちょうど 1 件のみであること（P1-2 — 複数の Start または複数の
   terminal Stop を持つ `agent_id` は候補から除外される、曖昧な chain を
   決して「最初の 1 件」で解決しない）
4. `SubagentStop` payload の `agent_transcript_path` が回収でき、かつそのパスが
   指すファイルが実在し・シンボリックリンクでなく・通常ファイルであり・
   （`transcript_allowed_roots` を指定した呼び出しでは）許可された root
   配下であり・サイズ上限以下であり・（`run_start_time`/`run_end_time` を
   指定した呼び出しでは）その時間窓内の mtime を持ち・読み取り可能・非空
   である（`agent_transcript_verified`。Issue #2183 AC11 / PR #2220 P1-1 —
   payload にパス文字列があるだけでは足りない）
5. 相関済み `agent_id` が、同一 `session_id`/`prompt_id` スコープ内の
   `Agent` tool_use/tool_result の `tool_use_id` 相関
   （`_claude_agent_tool_invocation_correlated`）で裏付けられており、かつ
   その tool_result が terminal かつ成功（`status == "completed"` かつ
   `is_error` でない）であること（`tool_invocation_id_correlated` と
   `terminal_tool_result_success` の両方が `True`。Issue #2183 AC3 / PR
   #2220 P0-3 — エラーや未完了（`async_launched` のまま）の tool_result は
   causal evidence として扱わない）
6. `expected_markers` を指定した呼び出しでは、その **すべての** 期待マーカー
   （従来の「いずれか 1 つ」ではない — PR #2220 P0-2 で強化）が、相関済み
   child 自身の `SubagentStop` の `last_assistant_message`（主たる provenance
   source）、または child 自身の transcript ファイルの assistant 発話record
   （JSONL としてパースし `type: "assistant"` の record のみを対象とする
   — user prompt／tool input record は対象外）由来であることが確認できる
   （`marker_provenance_verified`。Issue #2183 AC11/AC12 — parent 自身の
   最終応答文字列や child 自身の user prompt／tool input にのみ一致した
   場合は昇格しない）
7. 上記すべてを満たす候補 chain が **ちょうど 1 つ** であること（P0-3 —
   複数の `agent_id` がそれぞれ独立に全条件を満たす場合、どちらを選ぶかを
   決定できないため、いずれも昇格しない。この場合
   `causal_evidence_ambiguous_candidate_count` に候補数が記録される）

stdout/output の 2 チャンネルが同じ hook event の `agent_id`/`agent_type`/
`agent_transcript_path` について矛盾する値を報告した場合、そのイベントは
`contradictory: True` としてマークされ、いかなる派生フィールドも信用せず
相関に一切参加しない（PR #2220 P1-2）。

`marker_only_insufficient` は、hook lifecycle event が一切なく `expected_markers`
の文字列のみが `stdout` にある場合。それ以外（lone `SubagentStart`、
`agent_id` 不一致、session/prompt/type 不一致、transcript 未実在/空/
symlink/範囲外/サイズ超過/mtime 不整合、tool 相関なし、terminal でない
tool_result、marker provenance 未確認、候補 chain の曖昧さなど）はすべて
`no_evidence` になる。

### lane ごとの既定挙動（PR #2220 OWNER REQUEST_CHANGES P0-1 で再修正、
https://github.com/squne121/loop-protocol/pull/2220#issuecomment-5309790514）

`subagent_causal_evidence_verdict()` が構造的に評価できる hook lifecycle
channel は `--runtime claude --mode structured` の captured stdout にしか
存在しない。`causal_evidence_required` の判定はこの一点に明示的にスコープ
される:

```python
causal_evidence_required = args.require_subagent_causal_evidence or (
    args.runtime == "claude" and bool(args.expect_marker)
)
```

- **structured lane かつ `--runtime claude`**: 呼び出し側が `--expect-marker`
  を指定する場合、`causal_evidence_source == hook_id_correlated` は
  **既定強制**（追加フラグ不要）。`--expect-marker` を指定しない structured
  lane 実行にこの要件を課したい場合は `--require-subagent-causal-evidence`
  （既定 OFF）を明示する。
- **structured lane かつ `--runtime codex`**: Codex には
  `SubagentStart`/`SubagentStop` 相当の stream-json hook channel が存在せず
  `causal_evidence` は常に `None` になる。P0-1 修正前は
  `causal_evidence_required` が runtime を判別しなかったため、Codex が正常
  終了し `--expect-marker` の marker も出力していても `causal_evidence is
  None` を理由に exit 1 へ誤反転していた（バグ、意図した causal-evidence
  gap ではない）。修正後は `args.runtime == "claude"` を満たさない限り
  `causal_evidence_required` は `False` のままで、Codex の marker-only PASS
  はこの gate の対象外になる。
- **`--require-subagent-causal-evidence` の argparse-level 制約**: このフラグ
  を明示した場合、`--runtime claude --mode structured` 以外の組み合わせは
  `main()` が起動前に `parser.error`（exit 2）で拒否する。**interactive
  lane（`--mode interactive`）はこのフラグの opt-in 対象ではなくなった**
  （P0-1 fix-delta による破壊的変更 -- 従来 opt-in だった経路も含め、
  hook-lifecycle channel を構造的に持たない組み合わせでは常に拒否する）。
