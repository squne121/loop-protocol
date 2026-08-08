# コード分析: Claude Code custom SubAgent spawn observability（Issue #2013 AC1）

本文書は Issue #2013 AC1 の 3 点、すなわち (a) spawn-time / completion-time evidence の区分、
(b) `_run_route_once()` の実際の失敗判定評価順序（ファイルパス:行番号付き）、
(c) 拡張 diagnostic_cause taxonomy の定義、を記録する。

## 対象リビジョン（tested SHA と historical baseline SHA の両方を明記する）

- historical baseline SHA（PR #2005 merge commit）: `28394e226533cd59cdfc0f55602ac65e389a6600`
- actual tested SHA（本 research の実行時 `origin/main`）: `9eca2f0074552a3e0687b0c81ee94b62122890a0`
- Claude Code version: `2.1.225 (Claude Code)`

`28394e22` から `9eca2f00` までの差分は commit `9eca2f00`
「実装: codebase-investigator に pinned Graphify CLI advisory pilot を追加する (#2010)」の 1 件のみである。
provenance は次のとおりで、`scripts/agent-ops/run_worktree_agent_runtime_smoke.py` と
`scripts/agent-ops/run_agent_provider_route_smoke.py` はいずれも変更されていない。

| 変更ファイル | 追加行 |
| --- | --- |
| `.claude/agents/codebase-investigator.md` | 13 |
| `.claude/agents/tests/test_agent_contracts.py` | 26 |
| `.claude/skills/graphify-cli-advisory/SKILL.md` | 110 |
| `.claude/skills/graphify-cli-advisory/scripts/run_graphify_cli_advisory.py` | 694 |
| `.claude/skills/graphify-cli-advisory/tests/test_run_graphify_cli_advisory.py` | 823 |
| `.github/ci/python-test-plan.json` | 1 |

すなわち production lane の `codebase-investigator` は baseline 時点より
Graphify CLI advisory 前段（任意・read-only）を持つ定義になっている。
本 research の production lane は current main の定義をそのまま対象とする。

## (a) spawn-time evidence と completion-time evidence の区分

`scripts/agent-ops/run_worktree_agent_runtime_smoke.py`（tested SHA `9eca2f00`）の 3 つの
extractor は、stdout stream-json の異なるイベント種別・異なる時点の情報を読む。

### `extract_claude_parent_session_id()`（`run_worktree_agent_runtime_smoke.py:942-959`）

- 区分: **spawn-time evidence**
- 読むイベント: stdout stream-json の各行を先頭から走査し、`session_id` または `sessionId`
  キーを持つ **最初の** JSON object。
- 実測（本 research の raw evidence）: 実際には `type: "system"`, `subtype: "hook_started"`
  （`SessionStart` hook）が最初に `session_id` を持つ行として現れる。すなわち
  `system/init` より前に確定する、真に spawn 直前の evidence である。
- 失敗時: `None`（推測しない）。

### `extract_claude_child_agent_type()`（`run_worktree_agent_runtime_smoke.py:1035-1083`）

- 区分: **completion-time evidence**
- 読むイベント: `type: "user"` の event の top-level `tool_use_result.agentType`。
  構造化フィールドが無い場合のみ、同 event の `message.content[].content[].text` 内の
  `"agentType": "<name>"` テキスト断片へ正規表現 `_CLAUDE_AGENT_TYPE_RE`
  （`run_worktree_agent_runtime_smoke.py:1032`）でフォールバックする。
- 失敗時: `None`（fail-closed。欠落を一致として扱わない）。

### `extract_claude_child_session_id()`（`run_worktree_agent_runtime_smoke.py:1086-1121`）

- 区分: **completion-time evidence**（ただし引数として spawn-time evidence に依存する）
- 一次ソース: `_extract_claude_child_session_id_from_stream()`
  （`run_worktree_agent_runtime_smoke.py:962-1029`）が `type: "user"` event の
  `tool_use_result.agentId`、無ければ同 event の text block 内
  `agentId: <hex>` を `_CLAUDE_AGENT_ID_RE`（`run_worktree_agent_runtime_smoke.py:939`）で読む。
- 二次ソース: `~/.claude/projects/*/<parent_session_id>.jsonl` の persisted transcript。
  ただし structured lane は常に `--no-session-persistence` を渡す
  （`run_worktree_agent_runtime_smoke.py:483`）ため、この経路は構造的に成立しない。

### 既知の情報破壊欠陥（Issue 本文の記載を current code で再確認）

`run_worktree_agent_runtime_smoke.py:1100-1101`:

```python
    if not parent_session_id:
        return None
```

`parent_session_id` が欠落していると、stdout に child `agentId` が実在していても
`_extract_claude_child_session_id_from_stream()` を **一度も呼ばずに** `None` を返す。
spawn-time evidence の欠落が completion-time evidence の探索自体を潰す構造であり、
Issue 本文の記載どおりの欠陥が tested SHA でも維持されていることを確認した。

### `native_spawn_event_observed` の合成条件（`run_worktree_agent_runtime_smoke.py:2060-2074`）

```python
agent_type_identity_verified = (
    child_agent_type_observed is not None
    and requested_agent_type is not None
    and child_agent_type_observed == requested_agent_type
)
...
schema_summary["native_spawn_event_observed"] = bool(
    parent_session_id
    and child_session_id
    and parent_session_id != child_session_id
    and agent_type_identity_verified
)
```

すなわち `native_spawn_event_observed` は completion-time の `agentType` 一致に完全に依存する。

### 本 research が観測した runtime contract drift（current code を正とした差分記録）

Claude Code `2.1.225` の実測 raw evidence（`raw/control-01.stdout.jsonl` ほか全 30 trial）では、
`Agent` tool の `tool_use_result` は **非同期起動エンベロープ**であり、次の形をとる。

```json
{"isAsync": true, "status": "async_launched", "agentId": "a14b7e0673d997e52",
 "description": "...", "resolvedModel": "claude-sonnet-5", "prompt": "...", "outputFile": "..."}
```

`agentId` は存在するが **`agentType` フィールドは存在しない**。
テキストフォールバック（`"agentType": "<name>"`）も現れない。
したがって `extract_claude_child_agent_type()` は現行 runtime では常に `None` を返し、
`agent_type_identity_verified` は常に `False`、`native_spawn_event_observed` は
spawn が完全に観測できている run でも常に `False` になる。

一方、同じ stdout の hook channel には runtime 自身が返した identity evidence が存在する。

- `system/hook_started` および `system/hook_response` の `hook_event` が
  `"SubagentStart"` / `"SubagentStop"` を報告する。
- `SubagentStart` の `hook_name` は `"SubagentStart:<agent_type>"` 形式で agent type を含む。
- 公式 hook payload（`agent_id` / `agent_type` / `agent_transcript_path` / `stop_reason`）は
  hook command の stdin へ渡され、session-local な no-op logger hook が echo し返すことで
  `hook_response.stdout` として stream-json に現れる。

本 research の 30 trial すべてで、hook channel の `agent_id` と
`tool_use_result.agentId` は完全一致した（`cross_channel_identity_agreement.agent_id_channels_agree`）。
すなわち identity evidence は runtime から確かに提供されており、
欠けているのは repo 側の抽出経路のみである。

なお hook は唯一の ground truth として扱わない。upstream の
`https://github.com/anthropics/claude-code/issues/27755` は `SubagentStart`/`SubagentStop`
hook の未発火を報告する community bug report（"Closed as not planned"）であり公式契約ではない。
本 research は tool_use_result channel と hook channel の両方を独立に記録し、
両者の突き合わせ結果を `cross_channel_identity_agreement` として保存する設計を採る。

## (b) `_run_route_once()` の失敗判定の実際の評価順序

`scripts/agent-ops/run_agent_provider_route_smoke.py:523-552`（tested SHA `9eca2f00`）を
current code から再構成した評価順序は次のとおり。

| 順 | 条件 | ソース行 | `status` | `failure_class` |
| --- | --- | --- | --- | --- |
| 1 | `gemini_hits > 0` | `run_agent_provider_route_smoke.py:523` | `fail` | `gemini_invoked` |
| 2 | `fallback_hits > 0` | `run_agent_provider_route_smoke.py:526` | `fail` | `direct_fallback_invoked` |
| 3 | `harness_exit == 77` | `run_agent_provider_route_smoke.py:529` | `skip` | `agy_unavailable` |
| 4 | `harness_exit != 0` | `run_agent_provider_route_smoke.py:532` | `fail` | `validation_failed` |
| 5 | `not native_spawn_event_observed` | `run_agent_provider_route_smoke.py:535` | `fail` | `spawn_not_observed` |
| 6 | `request_validation != "pass"` | `run_agent_provider_route_smoke.py:538` | `fail` | `validation_failed` |
| 7 | `selected_provider != "agy"` | `run_agent_provider_route_smoke.py:541` | `fail` | `provider_mismatch` |
| 8 | `profile == "github_research" and route_evidence_sha256 is None` | `run_agent_provider_route_smoke.py:544` | `fail` | `route_evidence_schema_mismatch` |
| 9 | `not wrapper_ok` | `run_agent_provider_route_smoke.py:547` | `fail` | `validation_failed` |
| 10 | 上記いずれにも該当しない | `run_agent_provider_route_smoke.py:550` | `pass` | `None` |

### Issue 本文の記載との drift（current code を正とする）

Issue 本文の Notes for Reviewer は評価順序を 8 段階として記載し、
`route_evidence_schema_mismatch`（上表の順 8）を省いていた。
current code では順 7 と順 9 の間に `github_research` profile 限定の
`route_evidence_schema_mismatch` 判定が存在するため、実際は 9 段階である。
これは baseline SHA `28394e22` の時点で既に存在しており、
`9eca2f00` でも変更されていない（同 2 ファイルは `9eca2f00` の差分に含まれない）。
本 research の分類ロジックは current code の 9 段階を正として複製している。

### 順 4 が順 5 より先に評価されることの帰結

`harness_exit != 0`（順 4）は `native_spawn_event_observed`（順 5）より **先** に評価される。
`harness_exit` は `run_worktree_agent_runtime_smoke.py:2076-2107` の判定
（capability_skip / timed_out / rc is None / turn_limit_reached / rc != 0 /
terminal event 欠落 / expected marker 欠落）から決まる。
したがって marker 欠落など spawn とは無関係な理由で `harness_exit != 0` になった run は、
spawn evidence が有ろうと無かろうと一律 `validation_failed` に落ちる。
外側の `failure_class` だけからは spawn の有無を推測できない。

本 research では、この情報破壊を避けるため
`failure_class` を production と同一順序で複製しつつ、
それとは独立に raw lifecycle checkpoint から `diagnostic_cause` を算出する。

### `_is_transient_infrastructure_candidate()`（`run_agent_provider_route_smoke.py:394-395`）

```python
def _is_transient_infrastructure_candidate(route: dict[str, str], failure_class: str | None) -> bool:
    return route["runtime"] == "codex_cli" and failure_class == "spawn_not_observed"
```

`claude_code` + `spawn_not_observed` は明示的に bounded single retry の対象外である。
既存テスト `test_claude_spawn_not_observed_is_not_transient_candidate`
（`scripts/agent-ops/tests/test_agent_provider_route_smoke.py`）がこの契約を固定している。
評価結果は `retry-policy-assessment.md` に記録する。

## (c) 拡張 diagnostic_cause taxonomy の定義

既存の `failure_class` schema（`spawn_not_observed` / `validation_failed` ほか）は本 Issue では
一切変更しない。research artifact 内でのみ、次の 12 値の `diagnostic_cause` を lossless に付与する。
成功 trial の `diagnostic_cause` は `null` とする。

| diagnostic_cause | 定義（判定に用いる raw evidence） |
| --- | --- |
| `spawn_not_attempted` | プロセスは正常終了したが `Agent`/`Task` tool_use dispatch 自体が stdout に存在しない。 |
| `subagent_start_not_observed` | dispatch はあるが `SubagentStart` hook event も tool_result も観測されない。 |
| `subagent_completion_timeout` | dispatch はあるが completion 側 evidence（tool_result）が期限内に現れない、または wall-clock timeout（`system/api_retry` を伴わない）。 |
| `tool_result_identity_not_observed` | tool_result は存在するが `agentId` / `agentType` の identity evidence が揃わない。 |
| `agent_type_mismatch` | identity evidence は揃ったが observed agent type が requested agent type と一致しない。 |
| `runtime_api_retry_timeout` | wall-clock timeout かつ `system/api_retry` イベントが 1 件以上観測された。 |
| `runtime_nonzero` | runtime プロセスが非ゼロ終了した（dispatch 前・後を問わず該当段で評価）。 |
| `terminal_event_missing` | `type: "result"` の terminal event が stdout に存在しない。 |
| `marker_not_observed` | 期待 marker（`ROUTE_SMOKE_DONE` / `CONTROL_PROBE_DONE`）が stdout+stderr に現れない。 |
| `request_validation_failed` | delegation request の検証が `pass` でない（production は実 `delegation_request.json`、control は dispatch 時 `subagent_type` 一致）。 |
| `delegation_wrapper_failed` | delegation result wrapper が `ok` でない。 |
| `downstream_route_failed` | spawn lifecycle は成立したが AGY / Serena MCP / GitHub credential 等の downstream route が失敗した。 |

判定は `run_spawn_observability_trials.py` の `compute_diagnostic_cause()` に実装され、
`failure_class` とは独立に raw lifecycle checkpoint のみから算出される。
`system/api_retry` を伴う timeout（`runtime_api_retry_timeout`）と、
それを伴わない completion / tool-result / marker 欠落は明確に区別される。

## lifecycle 12 checkpoint

`reproduction-log.jsonl` の各 record は `lifecycle` に次の 12 個の boolean を持つ。
単一 boolean に潰さず、trial ID 単位で相関可能な形で独立記録する。

1. `process_started`
2. `system_init_observed`
3. `agent_tool_use_observed`
4. `subagent_start_hook_observed`
5. `subagent_stop_hook_observed`
6. `tool_result_observed`
7. `tool_result_agent_id_observed`
8. `tool_result_agent_type_observed`
9. `agent_type_matches_requested`
10. `terminal_event_observed`
11. `expected_marker_observed`
12. `delegation_request_validated`

`agent_type_matches_requested` は runtime が返した observed 値のみを用いる。
expected agent type の代入や agent 自身の self-report は identity evidence として扱わない。
