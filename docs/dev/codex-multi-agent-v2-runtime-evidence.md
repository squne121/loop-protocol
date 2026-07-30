---
id: codex-multi-agent-v2-runtime-evidence
status: final
summary_ja: "Codex Multi-Agent V2 移行 Phase 0（#1834）の runtime capability 調査結果。CLI 認識状況・isolated CODEX_HOME での config-loader accept/reject 証跡・passive recorder ログの相関可能フィールド調査。runtime capability（実際の spawn 動作）は未実証であることを明記する。"
related_issue: "#1834"
related_issues:
  - "#1833"
  - "#1830"
created: "2026-07-29"
updated: "2026-07-30"
---

# Codex Multi-Agent V2 runtime evidence（#1834）

本文書は parent #1833（Codex CLI Multi-Agent V2 移行）の Phase 0 child #1834 が生成した調査結果の記録である。実行環境（このリポジトリの `git worktree` 上、Codex CLI 0.146.0）で採取した config 認識証跡と、passive session recorder のログが parent-child spawn 相関に使えるフィールドを持つかどうかの読み取り専用調査結果をまとめる。

生成手順の正本は `scripts/agent-guards/probe_codex_v2_runtime_capability.py` であり、実行結果は `artifacts/codex-multi-agent-v2/runtime-capability.json` に記録される。本文書はその artifact の解釈と、artifact 化していない定性的調査（コードリーディング）の結果を記録する。

**この artifact が証明すること / しないこと（PR #1850 人間レビュー起因の訂正、必読）**:

- 証明すること: Codex CLI が `multi_agent_v2` feature 名を認識すること、そして isolated `CODEX_HOME` での隔離実行によって `[features.multi_agent_v2]` 構造化 config block を Codex 自身が実際に **accept/reject** すること（`v2_config_schema_loadable` / `config_loader_probe`）。
- 証明しないこと: 実際のエージェントセッションで `spawn_agent` V2 tool surface が呼び出し可能であること（**runtime capability は未実証**）。`runtime_exec_probe.status` は常に `not_run` であり、本 artifact の名前・本文書のタイトルにかかわらず、runtime capability の実証を主張しない。

## 1. Codex CLI version と `multi_agent_v2` feature flag 認識状況

- 実行環境の Codex CLI: `0.146.0`（`codex --version` → `codex-cli 0.146.0`）
- ambient `codex features list`（read-only、`.codex/config.toml` を書き換えない。ただし本リポジトリの `.codex/config.toml` は複数の named `[permissions.*]` profile を持つが top-level `default_permissions` を設定していないため、素の `codex features list` はこの無関係な config 検証エラーで exit 1 になる。これを避けるため ephemeral `-c default_permissions="loop-protocol-readonly"` override を付与し、かつ `cwd=repo_root` を明示している）は `multi_agent_v2` を **`stage: stable`, `enabled: false`** として認識する。
- 本リポジトリの `.codex/config.toml` には現時点で `[features]` テーブルが存在し（`hooks = true`）、`multi_agent_v2` の明示的な宣言はまだない（`check_config_toml_features_schema()` の `multi_agent_v2_form: absent`）。
- **`v2_config_schema_loadable` の定義を修正した**（PR #1850 人間レビュー finding #3）。旧実装は「TOML 構文が有効」かつ「ambient `codex features list` が `multi_agent_v2` を認識」の AND のみで判定しており、実際に Codex が `[features.multi_agent_v2]` の構造化 config block を受理するかを検証していなかった。新実装は、isolated（空の）`CODEX_HOME` を用意し、`cwd=repo_root` を明示した上で以下 4 ケースを実行する `probe_config_loader()` の結果に基づく:
  - `positive`: `-c features.multi_agent_v2.enabled=true -c features.multi_agent_v2.max_concurrent_threads_per_session=2` → **accept（exit 0）を期待**
  - `unknown_key_rejected`: 未知キーを追加 → **reject（exit 非 0）を期待**
  - `wrong_type_rejected`: `enabled` に文字列を指定 → **reject を期待**
  - `zero_concurrency_rejected`: `max_concurrent_threads_per_session=0` → **reject を期待**（ドキュメント上の最小値 1 未満）
  - `v2_config_schema_loadable` は、この 4 ケース全てが期待通りの accept/reject 結果になった場合（`config_loader_probe.status == "ok"`）にのみ `true` になる。

## 2. CLI 機能認識調査（spawn_agent V2 form / agent_type / task_name / fork_turns / nested delegation）

`codex --help` および `codex exec --help` のテキストを静的スキャンした結果、以下のトークンは **いずれも CLI のトップレベルオプションとしては見つからなかった**（`recognized: false`）:

- `spawn_agent`（V2 形式の spawn API）
- `agent_type` / `agent-type`
- `task_name` / `task-name`
- `fork_turns` / `fork-turns`
- `nested delegation`

これは、これらが CLI の起動時フラグではなく、エージェント実行時のツール呼び出しスキーマ（agent 内部で LLM が呼び出す `spawn_agent` 相当のツール定義）である可能性が高いことを示す。`codex --help` の静的スキャンだけでは、実際の agent runtime がこれらのツールを提供しているかどうかは確認できない。

**runtime capability は本 Issue では未実証（`runtime_exec_probe.status: not_run`）**。当初計画していた `codex exec --json` による非対話 canary、および Herdr 経由の TUI 対話 canary は、いずれも本 Issue のスコープでは実行しなかった。理由:

- 安全な read-only・no-network・no `.codex/hooks.json` 変更の範囲で、実際に tool surface の呼び出しを観測するには、少なくとも `.codex/hooks.json` へ一時的な観測用 hook を追加するか、write sandbox が必要になる可能性が高い。これは本 Issue の Stop Condition（`.codex/hooks.json`の変更が必要になる場合は無理に実行しない）に該当する。
- Herdr 経由の対話確認は時間対効果が低いため、明示的にスキップする（未実施であり「確認して runtime capability なし」ではない）。

したがって、**`spawn_agent` の V2 パラメータ形式（`agent_type` / `task_name` / `fork_turns`）と nested delegation の実際の runtime availability は本 Issue では未確認のまま**である。graph policy JSON・validator（Child 3）着手時には、実際に `features.multi_agent_v2` を有効化した状態でのライブ動作確認（別 Issue、runtime verification が `immediate` になる想定）が必要になる。

## 3. passive recorder ログの相関可能フィールド調査（`SubagentStop` / `SessionEnd`）

### 3.1 `.codex/hooks.json` の現在の wiring（訂正版）

**訂正（PR #1850 人間レビュー finding #2/#5）**: 旧版の本文書は「`SessionEnd` は `hooks.json` に一切登録されていない（`hooks.SessionEnd` キー自体が存在しない）」と記載していたが、これは誤りだった。現在の `.codex/hooks.json` には **`SessionEnd` と `SubagentStop` の両方**が `session-recording-composite.mjs` にそれぞれ wire されている（`timeout: 3` 秒）。

`probe_codex_v2_runtime_capability.py` の `build_hook_wiring()`（旧 `classify_recorder_generation()` の単一 enum を置き換えた構造化表現、finding #5）はこれを次のように artifact の `provenance.hook_wiring` へ記録する:

```json
{
  "status": "ok",
  "SessionEnd": {"present": true, "recorder_command": true, "timeout_seconds": 3},
  "SubagentStop": {"present": true, "recorder_command": true, "timeout_seconds": 3},
  "unexpected_events": []
}
```

旧 `recorder_generation` enum（`post_1830_advisory_subagent_stop_only` 等）は、`SessionEnd` と `SubagentStop` が同時に配線されている現在の状態を単一の値では表現できないため廃止した。

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
- **`SessionEnd` 経由の passive recorder ログは、上記の訂正の通り実際には `.codex/hooks.json` に配線されている**（3.1 節参照）。ただし、その hook input のフィールド構成（本節のテーブルと同じ確認が SessionEnd にも当てはまるか）は本調査では別途確認していない。

## 4. 証跡ファイルの privacy / freshness 契約

- artifact には絶対パス・利用者名を含めない。`resolve_codex_executable()` は `which_path` / `resolved_path` を返さず、代わりに `executable_basename` / `distribution_kind` / `target_triple` / `binary_sha256` のみを返す。全ての `raw_output` フィールドは `_sanitize_text()` を通し、`main()` は書き込み前に `find_privacy_violations()` で artifact 全体をスキャンし、違反があれば書き込みを拒否する（exit 3）。
- `provenance.input_digest_set` に `.codex/config.toml` / `.codex/hooks.json` / `scripts/session-recording/codex-hook-adapter.mjs` / probe script 自身の sha256 を保持する。`provenance.repo_head_sha` は生成時点の HEAD（この artifact 自身をまだ含まない、生成直前のコミット）を指す。
- `overall_status` は `pass | partial | fail` のいずれかであり、mandatory probe（`codex_cli_version` / `config_toml_parse` / `config_loader_probe`）のいずれかが失敗した場合は `fail` となり、`main()` は `--allow-partial` を明示しない限り非 0 exit する。artifact 自体は診断のため常に atomic rename で書き込まれる。

## 5. 後続 child への影響

- graph policy JSON・validator（Child 3）が parent-child spawn 相関を `SubagentStop` の passive recorder ログのみに依拠する設計を取る場合、`turn_id` の利用可否という未確認事項を前提として設計する必要がある。
- 現状の evidence からは、`agent_id` / `agent_type` / `session_id`（hook input 段階） / `agent_transcript_path`（hook input 段階、public artifact には残らない）の 4 フィールドが SubagentStop hook input で確実に利用可能である。`turn_id` を要件とする相関設計は追加のライブ確認（別 Issue）を経てから採用する。
- 後続 #1835（`.codex/config.toml` への `[features.multi_agent_v2]` table 追加）は、本 artifact の `v2_config_schema_loadable` / `config_loader_probe` を config 受理可否の根拠として利用できる。ただし **runtime capability（実際の spawn 可否）の根拠としては利用できない**（`runtime_exec_probe.status: not_run`）。
