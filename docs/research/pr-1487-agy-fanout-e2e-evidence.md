---
title: "fan-out orchestrator 実AGY/Serena MCP/WebSearch 並列実行 E2E 検証結果"
説明: "本文書は実機検証の最終結果をまとめた証跡文書である"
status: complete
related_issue: "#1494"
parent_issue: "#1265"
related_pr: "#1487"
review_reference: "https://github.com/squne121/loop-protocol/issues/1494#issuecomment-5071397001"
last_verified_run_utc: "2026-07-25T19:32:15.121150Z"
---

# fan-out orchestrator 実AGY/Serena MCP/WebSearch 並列実行 E2E 検証結果

## 1. 結論

**PASS**。`schema: AGY_FANOUT_E2E_VERDICT_V1` の validator（`validate_agy_fanout_e2e_evidence.py`、Issue #1710 / #1748 実装）による機械判定で、25 predicate 全てが `pass`（`predicate_09` は `requires_read_url_content: false` のため `not_applicable` 扱いだが failed 扱いではない）。

```json
{
  "schema": "AGY_FANOUT_E2E_VERDICT_V1",
  "schema_version": 1,
  "conclusion": "PASS",
  "status": "pass",
  "parent_run_id": "ae2e2fd7cdd64523bb568546e130f97e",
  "generated_at_utc": "2026-07-25T19:32:15.121150Z",
  "failed_predicates": [],
  "artifact_manifest_sha256": "aebfc70a292c01b22a395da7be3cb6a669b7f8a7bf40e5586b7ef14e494f60d1",
  "environment_manifest_sha256": "a141073dba0a5d7b4e9ed162b81e42d70dcfb2ba3ffe4bff91a1ea2cf7c23eab",
  "public_artifacts_redaction_status": "clean"
}
```

これは 2026-07-25 の live E2E 実行（本証跡 worktree の記録上「8 回目」の試行として扱われた実行）の結果である。直近の REQUEST_CHANGES レビュー（[#1494 comment 5071397001](https://github.com/squne121/loop-protocol/issues/1494#issuecomment-5071397001)）が指摘した 7 Blocker + 4 Major の全てに対応する implementation Issue が個別にマージ済みであり、本ドキュメントはその最終実行結果を記録する。

## 2. 実行環境・repository identity

| 項目 | 値 |
| --- | --- |
| 実行時点の origin/main HEAD（repository_sha） | `baa1e2f5ebf2a02b387f7aa535595d05234be20b`（#1771 merge） |
| 本証跡ドキュメント作成時点の origin/main HEAD | `baa1e2f5ebf2a02b387f7aa535595d05234be20b` |
| AGY version | `1.1.7` |
| AGY binary sha256 | `edec0312023c2e062e2080c8989ff86a74342c0fde6bd7e3e4cfb6528e795753` |
| authentication_state | `unknown`（environment manifest は認証 secret 値を保持しない設計。実行自体は認証済みセッションで成功しているため実質的に到達可能だったことは実行結果自体が示す） |
| is_wsl | `true` |
| os | `Linux` |
| python_version | `3.12.3` |
| locale / timezone | `C` / `JST` |
| Serena pinned ref | `3c241bab0d07d8adba8ef4f6c49676c954559dee` |
| Serena manifest hash | `4de10f721e6453bed2096598dce456d9d0b08dcd3a48928680180cd2f26fa702` |
| uv.lock hash | `3c1e1e514ae592ee78c4844f4ed181a397417a43b0650a09c5a0aeaad32f4482` |
| permission_policy_version | `1` |
| hook_schema_version | `1` |
| environment manifest schema | `agy_fanout_e2e_environment_manifest_v1` |
| environment_manifest_sha256 | `a141073dba0a5d7b4e9ed162b81e42d70dcfb2ba3ffe4bff91a1ea2cf7c23eab`（verdict と一致） |

証跡一式（`fanout_request.json` / `children/{local_asset_research,grounded_research,no_tools}/*` / `process_lifecycle_events.jsonl` / `environment_manifest.json` / `artifact_manifest.json` / `verdict.json`）は本タスク実行時点で `.claude/worktrees/issue-1494-final-e2e-attempt8/_scratch_1494_bundle_v9/`（HEAD `baa1e2f5`）に local 保存されている。当該 bundle はリポジトリへコミットしていない（raw evidence をリポジトリに直接混入させない、レビュー Major 3 の方針に整合）。本ドキュメントは同 bundle を読み取り、要約と hash 値のみを転記したものである。

## 3. 実装した implementation Issue 一覧（全 17 件、全 CLOSED）

`gh issue view <N> --json state` で個別に readback 済み。全件 `state: CLOSED`。

| # | Issue | 内容 | PR | merge commit |
| --- | --- | --- | --- | --- |
| predecessor | #1638 | AGY local_asset_research の targeted source evidence を fail-close で返す | #1720 | `ab4c006a5fcf1a54eb970f9f7576d14b7bb57d65` |
| A | #1708 | AGY PreToolUse hook から WebSearch provenance を正本化する（Blocker 1 対応） | #1714 | `65d44f570a6fffb10c5bc6a64ee1ed19b2a7646f` |
| B | #1707 | fan-out AGY provider process の lifecycle telemetry と実 overlap validator を追加する（Blocker 2 対応） | #1713 | `c45531da331eaa391fd5045a5c5f862702da6db0` |
| C | #1705 | AGY profile 別 isolated permission policy と no-tools negative evidence を導入する（Blocker 4 対応） | #1715 | `49945526826f108f5ea0d67d4d310e402aad5a55` |
| D | #1706 | fan-out local_asset_research の Serena evidence に task-linked hash chain と相関を導入する（Blocker 3 対応） | #1722 | `1df57fcbf9eeeeaa7f58ce7138b008fdb07365ee` |
| E | #1710 | AGY/Serena/WebSearch fan-out E2E artifact validator と environment manifest を追加する（Blocker 5/6 対応） | #1723 | `d9d88cf7f6d0f90ad0c275a1436a5fdd4e545b64` |
| F | #1726 | AGY isolated workspace が既存認証済みセッション（dbus）へ値露出なしで到達できるようにする | #1729 | `3c7a2397846f565193939e7370861ba425f2c270` |
| G | #1730 | AGY isolated workspace が gcloud ADC 認証キャッシュへ値露出なしで到達できるようにする | #1731 | `1bb6aa2f84a4caa09964a4be152ca08fb969a8e0` |
| H | #1740 | AGY isolated workspace が agy 固有 OAuth トークンファイルへ値露出なしで到達できるようにする | #1742 | `60b2d9ad3cac4540c6ba397e9966ca50c1d93df4` |
| I | #1743 | AGY OAuth トークンの isolated workspace 内 symlink 配置先を XDG_CONFIG_HOME から HOME 配下に修正する | #1746 | `6ef22c17877ddd887091ad15f8b34d23ca71c49a` |
| J | #1748 | fan-out E2E validator の schema 定数不一致と bundle 構築 script の欠落を解消する | #1750 | `3f25575011a474ea98e3a0f405df1c972f578162` |
| K | #1749 | agy headless print mode で grounded_research の search_web/read_url_content ツール呼び出しが発生しない原因を調査し、`grounded_research` を `--model claude-sonnet-4-6` 強制で対処する | #1751 | `b2352e973e637d7954aea73408a0fb530f5ab484` |
| L | #1752 | AGY PreToolUse hook events を isolated workspace 削除前に delegation_result/v1 へ配線し、grounded_research のツール呼び出し不発を再調査する | #1756 | `aa07986b250ba86d5324fa1ea2f173b82824bc57` |
| M | #1753 | delegation_result/v1 に parent_run_id/subtask_id/attempt_id を伝播させる | #1763 | `05a4c941f7bf04633a015a6109d93f102a269d38` |
| N | #1758 | isolated workspace で AGY の toolPermission 設定が原因で grounded_research の tool 実行が無人環境でスキップされる仮説を検証し対処する（反証+防御的修正） | #1769 | `5a7dbeec2e483b9e98da9bc7ca11043fd75b02c7` |
| - | #1768 | isolated workspace 内で search_web 成功時も agy_provenance_hook_events が常に空になる原因を特定する（hooks.json 配置先が `<workspace>/.agents/` ではなく `<HOME>/.gemini/config/` である根本原因を修正） | #1770 | `1d76c38fcd5a52d5548c7f1a594735206c2160f4` |
| O | #1771 | grounded_research 呼び出しに run_context を配線し hook provenance 相関を完成させる | #1772 | `baa1e2f5ebf2a02b387f7aa535595d05234be20b` |

## 4. Fan-out request における subtask 構成の public-safe な要約

`fanout_request.json`（schema: `agy_fanout_e2e_request_evidence_v1`）より。

```yaml
parent_run_id: ae2e2fd7cdd64523bb568546e130f97e
max_workers: 3
provider_concurrency:
  agy: 3
profile_concurrency:
  local_asset_research: 1
  grounded_research: 1
  no_tools: 1
subtasks:
  - subtask_id: local_asset_research
    provider: agy
    tool_profile: local_asset_research
    timeout_sec: 240
    objective: "fan_out_orchestrator.py の make_subprocess_runner と _run_one が process_lifecycle_event をどこで生成し、どの相関キー（artifact_stem/subtask_id/parent_run_id）で結び付けているかを、与えられたコード抜粋のみから説明する"
  - subtask_id: grounded_research
    provider: agy
    tool_profile: grounded_research
    requires_read_url_content: false
    timeout_sec: 480
    objective: "Google Antigravity (agy) の公式ドキュメントを1件 web 検索して、PreToolUse hook イベントのフィールド名、またはこの hook で観測される代表的な tool 名を1つ確認して報告し、末尾に machine-readable な引用JSON行を付与する"
  - subtask_id: no_tools
    provider: agy
    tool_profile: no_tools
    timeout_sec: 120
    objective: "inline context に書かれた算術式 2 + 2 の計算結果だけを、外部ツールを一切使わずに即答する"
```

3 subtask とも `provider: agy` で、明示的な concurrency 設定（`max_workers=3`, `provider_concurrency.agy=3`, 各 `profile_concurrency=1`）を持つ同一 fan-out request として同時実行された（レビュー Blocker 2 の「暗黙値に依存しない明示的な concurrency 設定」要求への対応）。

## 5. 実 AGY process overlap 計算結果（Blocker 2 対応・実装 Issue #1707）

`process_lifecycle_events.jsonl` より、3 subtask の `process_start` / `process_exit` イベント（executable: `run_gemini_headless.py`、いずれも `returncode: 0` / `termination_reason: exited_normally`）:

| subtask_id | pid/pgid（public-safe: distinct process identity のみ） | started_utc | exited_utc |
| --- | --- | --- | --- |
| local_asset_research | プロセス A（distinct pid） | 2026-07-25T19:28:33.539760Z | 2026-07-25T19:28:52.461683Z |
| grounded_research | プロセス B（distinct pid、A/C とは別 pid） | 2026-07-25T19:28:33.540000Z | 2026-07-25T19:29:13.553596Z |
| no_tools | プロセス C（distinct pid、A/B とは別 pid） | 2026-07-25T19:28:33.540060Z | 2026-07-25T19:28:46.376066Z |

3 プロセスとも `started_utc` が 19:28:33 台に集中しており、`no_tools`（プロセス C）が最も早く 19:28:46 に終了した後も、`local_asset_research`（プロセス A）・`grounded_research`（プロセス B）はそれぞれ 19:28:52・19:29:13 まで生存を継続した。すなわち [started_ns, exited_ns] の区間として、A/B/C いずれの組でも `max(start_a, start_b) < min(exit_a, exit_b)` が monotonic timestamp（`started_monotonic_ns` / `exited_monotonic_ns`）ベースで成立する。

Validator 判定: `predicate_06 (distinct_agy_process_overlap)` → `pass`（`evidence.overlap: true`, `evidence.pair_count: 3` — 3 プロセス全ての組み合わせで overlap が成立）。これは PID/PGID・`subprocess.Popen()` 実行後の実プロセス生存区間に基づく機械判定であり、レビュー Blocker 2 が指摘した「`subtask_started` は semaphore 取得後・`Popen()` 実行前に記録されるため実プロセス並列性を証明しない」という欠陥を、Issue #1707（process lifecycle telemetry）実装により解消したものである。

## 6. hook 由来の WebSearch 証跡（Blocker 1 対応・実装 Issue #1708 / #1710 / #1749 / #1752 / #1768 / #1771）

`children/grounded_research/hook_events.jsonl`（schema: `agy_tool_provenance_v1`, version 1）に記録された `PreToolUse` hook event 2 件:

```json
{"event": "PreToolUse", "conversationId": "1d832a71-0cc7-4fe9-bf76-85ba78c1a07f", "stepIdx": 3, "toolCall": {"name": "search_web", "args_sha256": "10d06b4d230f942d55ec9a5d8a8622a010638a325b51b467a379c48269c61fe9"}, "transcript_sha256": "b886a0f9ba3971b207e9c6ffbe6a70425473b2f8d9f7008d2ecac47b926dbbfa", "subtask_id": "grounded_research", "attempt_id": "attempt-1", "parent_run_id": "ae2e2fd7cdd64523bb568546e130f97e", "utc": "2026-07-25T19:28:50.644678Z"}
{"event": "PreToolUse", "conversationId": "1d832a71-0cc7-4fe9-bf76-85ba78c1a07f", "stepIdx": 6, "toolCall": {"name": "read_url_content", "args_sha256": "fb1185142c21be8a800a05bcd9bd7ac67a57e872a5bd9a325872e2857ae89a51"}, "transcript_sha256": "b886a0f9ba3971b207e9c6ffbe6a70425473b2f8d9f7008d2ecac47b926dbbfa", "subtask_id": "grounded_research", "attempt_id": "attempt-1", "parent_run_id": "ae2e2fd7cdd64523bb568546e130f97e", "utc": "2026-07-25T19:28:58.952682Z"}
```

これは AGY 自身の stdout 自己申告ではなく、Antigravity 公式 `PreToolUse` hook（`hooks.json`）が実際のツール呼び出し**前**に発火し、そのイベントを wrapper が捕捉・journal 化したものである。`toolCall.name` は現行 Antigravity ツール名である `search_web` / `read_url_content` そのものであり、レビューが指摘した旧式名称（`web_search`/`websearch` 等）の false-positive/false-negative 問題は解消されている。

以下は `grounding_backend` / `web_tool_call_count` / URL citation の実測値である（`children/grounded_research/result.json` の `grounded_research_evidence` フィールドより転記）:

```yaml
grounding_backend: agy_native_websearch
grounding_actor: antigravity_cli
grounding_status: grounded
search_query_count: 1
web_tool_call_count: 1
url_citation_count: 1
raw_transcript_included: false
raw_credential_included: false
redaction_status: checked_no_secret_pattern
```

Validator 判定:
- `predicate_07 (grounded_research_has_canonical_hook_event)` → `pass`（`matched_count: 2`, `validated_count: 2`）
- `predicate_08 (grounded_research_executes_search_web)` → `pass`（`tool_names: ["read_url_content", "search_web"]`）
- `predicate_09 (grounded_research_read_url_content_when_required)` → `not_applicable`（このリクエストは `requires_read_url_content: false` のため任意）
- `predicate_10 (hook_conversation_transcript_child_result_correlate)` → `pass`（`conversation_id_present: true`, `matched_count: 2`）
- `predicate_11 (stdout_self_report_alone_insufficient)` → `pass`（`matched_hook_events: 2`, `stdout_claims_search_web: false` — stdout 自己申告に依存しておらず hook event のみで成立していることを機械確認）

すなわち、正本は「AGY stdout の自己申告JSON」ではなく「Antigravity 公式 `PreToolUse` hook が同一 `conversationId` の transcript に紐づけて記録した実ツール呼び出しイベント」である。レビュー Blocker 1 の必須修正（hook/transcript を正本化し、stdout 自己申告を単独では成功条件にしない）を満たしている。

## 7. Serena の task 連携証跡チェーン（Blocker 3 対応・実装 Issue #1706）

`children/local_asset_research/serena_evidence.json` に記録された Serena MCP `tools/call` 実行 6 件（`find_file` / `search_for_pattern` / `get_symbols_overview` を各2回、`.claude/skills/gemini-cli-headless-delegation/scripts/fan_out_orchestrator.py` の `line_range: [1181, 1234]` を対象）:

```yaml
actor: wrapper_serena_mcp
serena_manifest_id: "serena_tool_manifest_v1:3c241bab0d07d8adba8ef4f6c49676c954559dee"
serena_pinned_ref: "3c241bab0d07d8adba8ef4f6c49676c954559dee"
source_kind: wrapper_read_only_targeted_evidence
repo_relative_path: ".claude/skills/gemini-cli-headless-delegation/scripts/fan_out_orchestrator.py"
selector: {kind: line_range, start_line: 1181, end_line: 1234}
parent_run_id: ae2e2fd7cdd64523bb568546e130f97e
subtask_id: local_asset_research
attempt_id: attempt-1
```

以下は hash chain（request → objective/target hash → Serena evidence → prompt envelope → child result binding）における各段階の実測 hash 値であり、request からの一連の対応関係を示す:

```yaml
request_sha256: 054c80c88a4a74cf54d3f1487d2746a0af2392f1063bb4fb58f3c316952d2c44
objective_sha256: e9476b10af605de9e3116251e051fe74338ece92476be52375ed70a0cecaefc5
target_contract_sha256: 642b0561c533550edfb3bd0feef907b80d7893f0e6e817e7ba3a212c4ad356aa
evidence_sha256: cd45414d5d0c434efc54770aefe35e355b886ee1f858b9507b579f10158ab0d4
prompt_envelope_sha256: c76de6bc053fad9a71af4ef6a310bb8d28b50e7d61dbc3945b8b1e729fa65a51
result_binding_sha256: c0fc027540083e926a149817cfd6f94739374cd77102294515fbe54f507c7865
```

`children/local_asset_research/result.json` の `local_asset_retrieval_metadata` にも同じ `evidence_sha256` / `request_sha256` / `objective_sha256` / `prompt_envelope_sha256` / `result_binding_sha256` が記録されており、Serena `tools/call` の evidence が同一 chain 上で AGY への prompt injection・child result に結び付いていることを確認できる。

Validator 判定:
- `predicate_12 (serena_evidence_task_linked)` → `pass`（`task_linked_count: 6`, `total_records: 6` — 全 6 件が task-linked）
- `predicate_13 (serena_hash_chain_verifies)` → `pass`（`checked: 6`）

対象範囲（`line_range: [1181, 1234]`）は fan-out request の `evidence_targets` として明示的に objective に紐づけられた selector であり（`fanout_request.json` の `evidence_targets` 参照）、固定 smoke query ではなく本 subtask の objective（`make_subprocess_runner` / `_run_one` の process lifecycle event 生成箇所の説明）に対応する task-specific selector である。これはレビュー Blocker 3 が指摘した「固定三呼出し・objective 非依存のハードコード検索語」問題への対応（実装 Issue #1706）である。

## 8. Actor 区別（retrieval_actor / analysis_actor）

```yaml
retrieval_actor: wrapper_serena_mcp
analysis_actor: antigravity_cli
agy_direct_mcp_access: false
```

Validator 判定: `predicate_14 (retrieval_and_analysis_actor_distinguished)` → `pass`（`evidence.analysis_actor: antigravity_cli`）。AGY は Serena MCP へ直接接続しておらず、wrapper（`run_gemini_headless.py`）が取得した bounded evidence のみを AGY prompt へ注入する設計（#1271 固定アーキテクチャ）が本実行でも維持されていることを確認した。「AGY が Serena MCP を使った」という誤記述は行わない。

## 9. 否定的証跡（no_tools / local_asset_research の AGY tool call ゼロ）

- `children/no_tools/result.json`: `agy_provenance_hook_events: []`（response_text は `"4"` — inline context の 2+2 計算結果のみを外部ツールなしで返答）
- `children/local_asset_research/result.json`: `agy_provenance_hook_events: []`（AGY 自身は直接ツールを呼んでおらず、Serena evidence は wrapper 経由の bounded evidence としてのみ prompt に注入されている）

Validator 判定:
- `predicate_15 (local_asset_research_agy_direct_tool_calls_zero)` → `pass`（`agy_direct_tool_calls_count: 0`）
- `predicate_16 (no_tools_agy_tool_calls_zero)` → `pass`（`agy_direct_tool_calls_count: 0`）

## 10. grounded_research における想定外 tool 呼び出しの件数

Validator 判定: `predicate_17 (grounded_research_unexpected_tool_calls_zero)` → `pass`（`unexpected_tool_calls_count: 0`）。`grounded_research` subtask で観測された PreToolUse hook event は `search_web` / `read_url_content` の 2 件のみであり、profile permission policy（実装 Issue #1705）が許可する範囲外の tool call は発生していない。

## 11. Audit ログの相関確認

本節における Validator 判定は次のとおりである:
- `predicate_18 (delegation_audit_start_end_one_to_one)` → `pass`（`pairing_problems: {}`。開始と終了の対応関係に問題なし）
- `predicate_19 (run_ids_consistent_across_all_artifacts)` → `pass`（`mismatches: {}`。全成果物の識別子が一致）
- `predicate_23 (all_child_results_satisfy_success_condition)` → `pass`（`failures: {}`。失敗した子実行なし）

`parent_run_id: ae2e2fd7cdd64523bb568546e130f97e` が `fanout_request.json` / `process_lifecycle_events.jsonl` / 各 child `audit.jsonl` / `request.json` / `result.json` / `serena_evidence.json` / `hook_events.jsonl` の全 artifact で一致しており、`delegation_audit.jsonl` の start/end イベントも 1:1 で対応する（実装 Issue #1753: `parent_run_id`/`subtask_id`/`attempt_id` の伝播）。

## 12. Artifact manifest hash と validator 出力（25 predicate 全内訳）

`artifact_manifest.json`（17 artifact、SHA-256）の内容は以下のとおりであり、全て bundle 読み取り時に fail-closed で検証済みである:

```json
{
  "children/grounded_research/audit.jsonl": "ba8d547af06c8559681336f6c4e5e6b73d8da7420d2c636c7db64419e11a52fc",
  "children/grounded_research/hook_events.jsonl": "c174ebf95bb632f77e7f4f970dda3d2106aecd06d1609566d31c3970e1863cce",
  "children/grounded_research/permission_events.json": "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
  "children/grounded_research/request.json": "de52b7db505c9694c702f6d5142869d65f7377d188eb6a83470000e6bcf6a891",
  "children/grounded_research/result.json": "c9cfa0d62c10276a52c5524a282bf62f341c7088037c9b567ea1d565dc410314",
  "children/local_asset_research/audit.jsonl": "36ce01e9fc9e86076c85a9ac011abc26ee363134c8985c031501ca0fb45b6bb2",
  "children/local_asset_research/permission_events.json": "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
  "children/local_asset_research/request.json": "35fd2bdfcc1fe9f1d8fadf7819814e326b1d0ab5bf559611f8f8a93a6ca094ac",
  "children/local_asset_research/result.json": "5dd1ef4fa96f250fa512af76fe33d6b22860e20898c1e7d661ff2c67cc9413c0",
  "children/local_asset_research/serena_evidence.json": "0071e8b12501f8466cd658a514aa481bd5bbe9ea787ac94de69543fc876adee3",
  "children/no_tools/audit.jsonl": "34c9280eefb968a5b79968381c22ab96989300194a0d4f3967a245ffa9c67eef",
  "children/no_tools/permission_events.json": "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
  "children/no_tools/request.json": "7c4481151aa534917905c710d948562ee6ae91df6d2e8950463e144c8466ad36",
  "children/no_tools/result.json": "3e34452dbd45dbf53c9accaf105e4230bc498c002acaab4eaa724925d097dc93",
  "environment_manifest.json": "54fb7f2072d505edc26f3c677a5bdaad797d8f38916cc7bc80202fde42397aa9",
  "fanout_request.json": "fdeb3a2c37a8713061a850db8e22da75010db44325d18ba059cca322289659f2",
  "process_lifecycle_events.jsonl": "f6d937b5ffba0fb3fea2a6ad674eea06e4b1fe010eb656d9b6fafe61e65f5fae"
}
```

`artifact_manifest_sha256: aebfc70a292c01b22a395da7be3cb6a669b7f8a7bf40e5586b7ef14e494f60d1`（この manifest 自体の hash。`verdict.json` と一致。実装 Issue #1710 / #1748）

Validator（`AGY_FANOUT_E2E_VERDICT_V1`, schema_version 1）25 predicate 全内訳:

| predicate_id | name | status | evidence 要約 |
| --- | --- | --- | --- |
| predicate_01 | fanout_request_parent_run_id_consistent | pass | mismatched_subtasks: [] |
| predicate_02 | unique_subtask_count_exactly_3 | pass | subtask_ids: local_asset_research, grounded_research, no_tools |
| predicate_03 | profile_set_exact | pass | profiles: grounded_research, local_asset_research, no_tools |
| predicate_04 | provider_all_agy | pass | providers: [agy, agy, agy] |
| predicate_05 | concurrency_explicit | pass | max_workers=3, provider_concurrency.agy=3, profile_concurrency 各1 |
| predicate_06 | distinct_agy_process_overlap | pass | overlap=true, pair_count=3 |
| predicate_07 | grounded_research_has_canonical_hook_event | pass | matched_count=2, validated_count=2 |
| predicate_08 | grounded_research_executes_search_web | pass | tool_names: [read_url_content, search_web] |
| predicate_09 | grounded_research_read_url_content_when_required | not_applicable | requires_read_url_content=false |
| predicate_10 | hook_conversation_transcript_child_result_correlate | pass | conversation_id_present=true, matched_count=2 |
| predicate_11 | stdout_self_report_alone_insufficient | pass | matched_hook_events=2, stdout_claims_search_web=false |
| predicate_12 | serena_evidence_task_linked | pass | task_linked_count=6, total_records=6 |
| predicate_13 | serena_hash_chain_verifies | pass | checked=6 |
| predicate_14 | retrieval_and_analysis_actor_distinguished | pass | analysis_actor=antigravity_cli |
| predicate_15 | local_asset_research_agy_direct_tool_calls_zero | pass | agy_direct_tool_calls_count=0 |
| predicate_16 | no_tools_agy_tool_calls_zero | pass | agy_direct_tool_calls_count=0 |
| predicate_17 | grounded_research_unexpected_tool_calls_zero | pass | unexpected_tool_calls_count=0 |
| predicate_18 | delegation_audit_start_end_one_to_one | pass | pairing_problems={} |
| predicate_19 | run_ids_consistent_across_all_artifacts | pass | mismatches={} |
| predicate_20 | artifact_manifest_sha256_matches | pass | artifact_count=17（bundle load 時に fail-closed 検証済み） |
| predicate_21 | no_raw_secrets_in_public_artifacts | pass | violations={} |
| predicate_22 | redaction_scanner_passes_all_public_artifacts | pass | required_count=17, scanned_count=17 |
| predicate_23 | all_child_results_satisfy_success_condition | pass | failures={} |
| predicate_24 | fail_closed_on_missing_duplicate_unknown_artifact | pass | bundle load 時に fail-closed 検証済み |
| predicate_25 | tampering_detected_via_hash_chain_and_manifest | pass | artifact_manifest sha256 と Serena hash chain の両方が検証された場合にのみ到達 |

`public_artifacts_redaction_status: clean`（全 17 public artifact が redaction scanner を通過。secret pattern 違反なし）。

## 13. Issue #1494 Acceptance Criteria との対応

| AC | 内容 | 対応箇所 |
| --- | --- | --- |
| AC1 | `delegation_fanout_result_v1` の `status` が記載されている | 本ドキュメント §1・§11（`all_child_results_satisfy_success_condition: pass`, 3 child とも `fanout_status: succeeded`。§6の process_lifecycle_events.jsonl 参照） |
| AC2 | `manifest.json` / `events.ndjson` / `delegation_audit.jsonl` 相当の要約と保存先 | 本ドキュメント §12（`artifact_manifest.json` 全17件hash）、§11（audit correlation）。保存先は §2 に明記の worktree local path（bundle 自体はリポジトリ非コミット） |
| AC3 | 2件以上の `subtask_started` が最初の `subtask_finished` より前に記録されていること（かつ実装Issue #1707 により実 process overlap も証明） | 本ドキュメント §5（`predicate_06`, 3プロセス全組み合わせで overlap） |
| AC4 | `grounded_research` の `grounding_backend`/`web_tool_call_count`/実URL citation。stdout自己申告への依存が明記されている（かつ実装Issue #1708によりhook正本化） | 本ドキュメント §6（hook-derived proof。`predicate_11`でstdout自己申告非依存を機械確認） |
| AC5 | `local_asset_research` の Serena `tools/list`/`tools/call` public-safe evidence。固定probe制限の明記（かつ実装Issue #1706により task-linked化） | 本ドキュメント §7（`predicate_12`/`predicate_13`、task-specific selector） |
| AC6 | Issue #1273 acceptance criteria への運用可能性分析結論とGap 1-4対応implementation Issueのリンク | 本ドキュメント §3（17件のimplementation issue一覧）、§14（review blocker/major対応） |

Gap 1（WebSearch hook正本化）・Gap 2（process lifecycle telemetry）・Gap 3（task-linked Serena query）・Gap 4（profile別 tool deny）は、それぞれ #1708、#1707、#1706、#1705 で解消済みである。加えて、この過程で新たに判明した runtime 固有の不具合（isolated workspace の認証到達性 #1726/#1730/#1740、symlink配置先 #1743、validator schema定数不一致 #1748、grounded_researchのtool呼び出し不発の原因調査・修正 #1749/#1752/#1758/#1768、run_context配線完成 #1771）についても全て implementation Issue として起票・実装・マージ済みである。

## 14. レビュー #issuecomment-5071397001 の Blocker/Major への回答

### Blocker 1 — WebSearch証跡の自己申告依存
**対応済み**（実装 Issue #1708 / #1749 / #1752 / #1768 / #1771）。正本を AGY stdout の marker line 自己申告から `PreToolUse` hook event（`toolCall.name`, `conversationId`, `transcript_sha256`）へ変更した。§6 の `predicate_11 (stdout_self_report_alone_insufficient)` が、hook event のみで成立し stdout 自己申告に依存していないことを機械検証している。旧式ツール名（`web_search`/`websearch`等）ではなく現行の `search_web`/`read_url_content` を正しく認識する。

なお、hook を実際に発火させるための `hooks.json` 配置先バグ（isolated workspace 内 `.agents/hooks.json` ではなく、実際の discover パスである `<HOME>/.gemini/config/hooks.json` に配置する必要があった）は #1768 で根本原因を特定し、#1771 で run_context 配線を完成させた。

### Blocker 2 — concurrency gate がプロセス並列実行を証明しない
**対応済み**（実装 Issue #1707）。`subtask_started`/`subtask_finished` journal event に加え、実 `subprocess.Popen()` の PID/PGID/monotonic timestamp を記録する `process_start`/`process_exit` event（schema: `process_lifecycle_event_v1`）を追加し、`max(a.started_ns, b.started_ns) < min(a.exited_ns, b.exited_ns)` による overlap validator を実装した。§5 参照。fan-out request にも `max_workers`/`provider_concurrency`/`profile_concurrency` を明示（§4）。

### Blocker 3 — Serena実呼出しがtask objectiveと無関係な固定probe
**対応済み**（実装 Issue #1706）。`request_sha256`/`objective_sha256`/`target_contract_sha256`/`evidence_sha256`/`prompt_envelope_sha256`/`result_binding_sha256` の hash chain を追加し、Serena evidence を task の `evidence_targets`（objective-derived selector）と暗号学的に結び付けた。§7 参照。`retrieval_actor: wrapper_serena_mcp` / `analysis_actor: antigravity_cli` の区別も明示（§8）。

### Blocker 4 — profile別tool denyが技術的に強制されていない
**対応済み**（実装 Issue #1705）。AGY profile ごとの permission policy を runtime gate として実装し、no-tools negative evidence を導入した。§9（`predicate_15`/`predicate_16`: `agy_direct_tool_calls_count: 0`）、§10（`predicate_17`: `unexpected_tool_calls_count: 0`）で機械検証済み。

### Blocker 5 — rgベースVerification Commandsのfalse-green問題
**対応済み**（実装 Issue #1710 / #1748）。`validate_agy_fanout_e2e_evidence.py` による 25 predicate の machine-readable validator（`AGY_FANOUT_E2E_VERDICT_V1`）を実装し、artifact SHA-256一致・run/subtask/attempt_id相関・process overlap・hook由来WebSearch・Serena evidence hash一致・AGY tool calls ゼロ・unexpected tool call ゼロ・redaction scanner通過を全て機械判定する。本ドキュメントの §12 に全 predicate の内訳を記載。docs-only の Allowed Paths を守りつつ、validator自体は別 implementation Issue（#1710/#1748）で追加し、#1494 の本ドキュメントはその出力を引用する構成にした。

### Blocker 6 — 配布物・設定・認証環境が固定されていない
**対応済み**（実装 Issue #1710）。`environment_manifest.json`（schema: `agy_fanout_e2e_environment_manifest_v1`）に `agy_version`/`agy_binary_sha256`/`repository_sha`/`serena_pinned_ref`/`serena_manifest_hash`/`uv_lock_hash`/`permission_policy_version`/`hook_schema_version`/`is_wsl`/`os`/`python_version` を記録し、`environment_manifest_sha256` として verdict に紐付けた（§2, §12）。OAuth URL・token・HOME絶対パス等の値そのものは記録せず、`authentication_state` は presence 情報のみとした。

### Blocker 7 — 一回のhappy-path runでAC4/7/8/11/16全体を証明してはいけない
**部分対応・明示的スコープ限定**。本ドキュメントが証明する範囲は、レビューが示した表の「happy-path E2Eで分かること」の列（fan-out entrypoint起動・overlap telemetryによるconcurrency・通常完了時のaudit correlation・E2Eが一度通ったこと）に限定される。**timeout/cancellation（deadline時のSIGTERM/SIGKILL、late result discard）は本実行では `not_tested` である**。意図的に遅いchildを使うnegative control run（`overall_timeout_sec`短縮によるタイムアウト誘発）は #1494 のスコープ外（Allowed Pathsがdocsのみのため）とし、別途 follow-up の検証範囲とする。本ドキュメントはこの限定を明示することで、レビュー指摘の「timeout contractの証明への不当な昇格」を行わない。

### Major 1 — prompt truncation/fabricationの検査
**部分対応**。本実行では `transcript_sha256`（`grounded_research`: `b886a0f9ba3971b207e9c6ffbe6a70425473b2f8d9f7008d2ecac47b926dbbfa`）により transcript の tamper-evidence は確保しているが、明示的な `tail_sentinel`/`sentinel_present_in_transcript` フィールドは本 request では使用していない。3 subtask のprompt本文はいずれも短く（数百〜数千文字オーダー）、旧版で報告された約48KB規模のtruncation閾値を大きく下回るため、本実行においてtruncationのリスクは実質的に低いと判断する。将来的な長大prompt実行時のtruncation検査は follow-up スコープとする。

### Major 2 — 単発成功とflakinessの区別
**部分対応・スコープ制限の明示**。本ドキュメントが記録するのは単一の live E2E実行（2026-07-25、8回目の試行として扱われた実行）の結果である。この実行に至るまでに複数回の試行・修正サイクル（実装Issue #1749/#1752/#1758/#1768/#1771など）を要しており、それぞれの過程で `local_code_defect`（例: hooks.json配置先バグ）、`local_instrumentation_missing`（例: run_context未配線）に該当する失敗を identify・修正した記録が各 issue に残っている。同一設定での複数回repetitionによるflakiness測定（`repetitions.attempted >= 2`）は本ドキュメントでは実施しておらず、follow-up スコープとする。

### Major 3 — 公開証跡とprivate raw evidenceの分離
**対応済み**。本ドキュメントは validator出力・artifact manifest hash・要約のみを含み、raw stdout/stderr全文・OAuth URL・HOME絶対パス・prompt全文は掲載していない。raw evidence bundle（`_scratch_1494_bundle_v9/`, `_scratch_1494_run_v9/`）はリポジトリにコミットせず、worktree local に保持している。`grounded_research_evidence.raw_transcript_included: false` / `raw_credential_included: false`（§6参照）。

### Major 4 — 実行後の非変更・cleanup確認
**対応済み（本タスク範囲内で確認）**。本ドキュメント作成用の worktree（`issue-1494-evidence-doc`）は origin/main から新規作成した clean worktree であり、証跡読み取りに使用した `issue-1494-final-e2e-attempt8` worktree への書き込みは行っていない。Allowed Paths（`docs/research/pr-1487-agy-fanout-e2e-evidence.md` のみ）以外の tracked file 変更は行っていない。

## 15. Secret non-emission / external billing / raw transcript 非掲載の宣言

- 本ドキュメントには OAuth token、API key、credential 値、HOME 絶対パス、raw AGY stdout/stderr 全文、raw Serena MCP transcript 全文を一切含めていない。
- 全て hash 値（SHA-256）・件数・boolean フラグ・public-safe な要約テキストのみを転記している。
- Validator 自体が `predicate_21 (no_raw_secrets_in_public_artifacts)` / `predicate_22 (redaction_scanner_passes_all_public_artifacts)` により、参照元の 17 public artifact 全件について secret pattern 違反なしを機械確認している（`public_artifacts_redaction_status: clean`）。
- 本検証は既存の認証済み AGY / gcloud ADC セッションを使用しており、追加の外部課金（billing）は発生していない（AGY呼び出しは既存サブスクリプション/認証枠内、Serena MCPはローカルプロセス、WebSearchはAGYネイティブ機能）。

## 16. 最終結論

**PASS**。#1265 の並列実行評価観点における「AntigravityCLI (agy) provider が fan-out orchestrator の並列実行先として実運用可能であるか」という問いに対し、実 AGY・実 Serena MCP・実 AGY native WebSearch を用いた fan-out E2E 実行で、25 predicate 全て（1件 not_applicable、他は全て pass）を満たす機械検証済みの肯定的結論を得た。Blocker 1-6 は完全対応、Blocker 7・Major 1-2 は本実行のスコープ限定を明示した上で部分対応、Major 3-4 は対応済みである。timeout/cancellation の negative-control run および複数回 repetition による flakiness 測定は本ドキュメントのスコープ外（`not_tested`）であり、必要であれば follow-up Issue で扱う。
