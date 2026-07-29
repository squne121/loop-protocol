---
id: codex-multi-agent-v2-runtime-evidence
status: draft
summary_ja: "Codex Multi-Agent V2 移行 Phase 0（#1834）の runtime capability 調査結果。CLI 認識状況と passive recorder ログの相関可能フィールド調査。"
related_issue: "#1834"
related_issues:
  - "#1833"
  - "#1830"
created: "2026-07-29"
---

# Codex Multi-Agent V2 runtime evidence（#1834）

本文書は parent #1833（Codex CLI Multi-Agent V2 移行）の Phase 0 child #1834 が生成した調査結果の記録である。実行環境（このリポジトリの `git worktree` 上、Codex CLI 0.146.0）で採取した runtime capability 証跡と、passive session recorder のログが parent-child spawn 相関に使えるフィールドを持つかどうかの読み取り専用調査結果をまとめる。

生成手順の正本は `scripts/agent-guards/probe_codex_v2_runtime_capability.py` であり、実行結果は `artifacts/codex-multi-agent-v2/runtime-capability.json` に記録される。本文書はその artifact の解釈と、artifact 化していない定性的調査（コードリーディング）の結果を記録する。

## 1. Codex CLI version と `multi_agent_v2` feature flag 認識状況

- 実行環境の Codex CLI: `0.146.0`（`codex --version` → `codex-cli 0.146.0`）
- `codex features list`（read-only、`.codex/config.toml` を書き換えない）は `multi_agent_v2` を **`stage: stable`, `enabled: false`** として認識する。CLI はこの機能を feature flag として正式に認識しているが、既定では無効化されている。
- `codex -c features.multi_agent_v2=true features list`（ephemeral override、`.codex/config.toml` へは非永続）を実行すると `multi_agent_v2` の `enabled` が `true` に切り替わることを確認した。これは CLI が `features.multi_agent_v2` という config key path を構文的に受理できることの直接証拠である。
- 本リポジトリの `.codex/config.toml` には現時点で `[features]` テーブル自体が存在しない（`multi_agent_v2` の明示的な宣言なし）。`v2_config_schema_loadable` は「config.toml の TOML 構文が有効（テーブル未宣言でも valid）」かつ「CLI が `multi_agent_v2` を既知の feature として認識する」の両方を満たす場合に `true` とする設計にした（artifact の `v2_config_schema_loadable` フィールド）。

## 2. CLI 機能認識調査（spawn_agent V2 form / agent_type / task_name / fork_turns / nested delegation）

`codex --help` および `codex exec --help` のテキストを静的スキャンした結果、以下のトークンは **いずれも CLI のトップレベルオプションとしては見つからなかった**（`recognized: false`）:

- `spawn_agent`（V2 形式の spawn API）
- `agent_type` / `agent-type`
- `task_name` / `task-name`
- `fork_turns` / `fork-turns`
- `nested delegation`

これは、これらが CLI の起動時フラグではなく、エージェント実行時のツール呼び出しスキーマ（agent 内部で LLM が呼び出す `spawn_agent` 相当のツール定義）である可能性が高いことを示す。`codex --help` の静的スキャンだけでは、実際の agent runtime がこれらのツールを提供しているかどうかは確認できない。`codex features list` では `multi_agent`（stable, true, 既存 V1）と `multi_agent_v2`（stable, false）の 2 つの機能フラグが存在することのみが確認できた。

**後続 child への影響**: `spawn_agent` の V2 パラメータ形式（`agent_type` / `task_name` / `fork_turns`）と nested delegation の実際の availability は、CLI ヘルプテキストからは確認できない。graph policy JSON・validator（Child 3）着手時には、実際に `features.multi_agent_v2=true` を有効化した状態でのライブ動作確認（別 Issue、runtime verification が `immediate` になる想定）が必要になる可能性が高い。

## 3. passive recorder ログの相関可能フィールド調査（`SubagentStop` / `SessionEnd`）

### 3.1 `.codex/hooks.json` の現在の wiring（#1830 マージ後）

`.codex/hooks.json` には `SubagentStop` の handler として `session-recording-composite.mjs --event SubagentStop` が wire されている。`SessionEnd` は **`hooks.json` に一切登録されていない**（`hooks.SessionEnd` キー自体が存在しない）。したがって、現時点で live に採取できる passive recorder ログは `SubagentStop` 経由のもののみであり、`SessionEnd` 経由の recorder ログは存在しない（#1830 が SessionEnd passive recorder を追加する想定だったが、実際のマージ結果では `SubagentStop` のみが wire されている — Issue #1834 の Dependency 節が言及する「SessionEnd passive recorder」は本リポジトリの現状には存在しないため、以降の child は `SubagentStop` のみを前提にする必要がある）。

`probe_codex_v2_runtime_capability.py` の `classify_recorder_generation()` はこの構造を `post_1830_advisory_subagent_stop_only` として artifact の `provenance.recorder_generation` に記録する。

### 3.2 フィールドごとの所在確認

コードリーディング（read-only）で以下を確認した。

| フィールド | SubagentStop hook input | passive recorder 出力（`.txt` manifest） | 備考 |
|---|---|---|---|
| `agent_id` | **あり**（`.claude/hooks/generate_session_manifest_from_hook.mjs`: `hookCtx?.agent_id ?? hookCtx?.subagent_id ?? null`） | 未確認（producer CLI 引数へは渡るが public manifest content への転記は未確認） | hook wrapper が producer CLI 引数へ変換する対象 |
| `agent_type` | **あり**（`.claude/hooks/capture_scope_rollup_final_response.py`: `payload.get("agent_type")` を SubagentStop payload から直接読み取り） | 不明（scope-rollup capture は診断 sidecar のみで canonical manifest とは別 producer） | scope-rollup capture の target agent 判定に使用 |
| `session_id` | **あり**（`generate_session_manifest_from_hook.mjs`: `hookCtx?.session_id ?? null`） | manifest の `phase_instance_id` 構築要素として間接的に使用（`issue-<N>:<phase>:<seq>` 形式のため raw session_id は manifest 本体に直接転記されない） | public-safe contract により transcript_path/cwd 絶対パスは除外されるが session_id 自体の扱いは producer 実装依存 |
| `turn_id` | **SubagentStart では確認済み**（`scripts/check-codex-agents.mjs` の `appendLaunchEvidence()`: `payload.turn_id ?? null`、`scripts/check_subagent_launch_ledger.py` の launch ledger schema）。**SubagentStop での存在は本調査では確認できなかった** — `turn_id` を読み取るコンシューマは `SUBAGENT_LAUNCH_LEDGER_V1`（`SubagentStart` 専用、`scripts/check_subagent_launch_ledger.py:144` が `event_type != "SubagentStart"` を拒否）のみで、`SubagentStop` 側のコンシューマに `turn_id` 読み取りコードは見つからなかった | Codex hook payload のフィールド集合はイベント種別ごとに異なる可能性があり、`SubagentStart` に存在するフィールドが `SubagentStop` にも存在するとは限らない（未確認事項） |
| `agent_transcript_path` | **あり**（`.claude/hooks/capture_scope_rollup_final_response.py`: `payload.get("agent_transcript_path")`） | 含まれない（`transcript_path` / `cwd` の絶対パスは public output から明示的に除外、`docs/dev/session-recording-policy.md` 771行目） | scope-rollup capture の診断 sidecar には digest 化された形で記録される可能性があるが、絶対パス自体は public-safe contract により non-public |
| `tool_use_id` | **あり**（`generate_session_manifest_from_hook.mjs`: `hookCtx?.tool_use_id ?? null`、ただしコメントで「producer CLI への引数化は未実装、phase-instance-id へのエンコードに使う想定」と明記） | 未確認 | |

### 3.3 結論（含む/含まないの明記）

- `agent_id` / `agent_type` / `session_id` / `agent_transcript_path` の **4 フィールドは `SubagentStop` hook input に含まれる**ことをコードリーディングで確認した（複数の既存 consumer が `payload.get(...)` / `hookCtx?.xxx` で直接読み取っている）。
- `turn_id` は `SubagentStop` hook input に含まれるかどうか **本調査では確認できなかった**（`SubagentStart` にのみ存在することが確認された既存コンシューマがある一方、`SubagentStop` 側のコンシューマは見つからなかった）。graph policy / task packet 実装（Child 3/4）で `turn_id` を parent-child 相関キーとして使う場合は、実際の `SubagentStop` hook payload（live または fixture）を独立に確認する追加調査が必要。
- passive recorder の **canonical public 出力（`.txt` manifest）自体には、`transcript_path` / `cwd` の絶対パスが明示的に除外される**ため、`agent_transcript_path` を相関キーとして使う場合は hook input 段階（producer 実行前の中間データ）でのみ利用可能であり、public artifact には残らない。
- `session_id` は hook input には存在するが、canonical manifest では `phase_instance_id`（`issue-<N>:<phase>:<seq>` 形式）に変換されて格納されるため、raw `session_id` そのものを manifest から逆引きすることは想定されていない。
- **`SessionEnd` 経由の passive recorder ログは本リポジトリの現状の `.codex/hooks.json` には存在しない**（`SessionEnd` キー自体が未登録）ため、`SessionEnd` hook input のフィールド構成は本調査では確認できなかった（対象が存在しない）。

### 3.4 後続 child への影響

- graph policy JSON・validator（Child 3）が parent-child spawn 相関を `SubagentStop` の passive recorder ログのみに依拠する設計を取る場合、`turn_id` の利用可否と `SessionEnd` 未配線という 2 点の未確認事項を前提として設計する必要がある。
- 現状の evidence からは、`agent_id` / `agent_type` / `session_id`（hook input 段階） / `agent_transcript_path`（hook input 段階、public artifact には残らない）の 4 フィールドが SubagentStop hook input で確実に利用可能である。`turn_id` を要件とする相関設計は追加のライブ確認（別 Issue）を経てから採用する。
