# retry policy 評価（Issue #2013 AC4）

`scripts/agent-ops/run_agent_provider_route_smoke.py` の
`_is_transient_infrastructure_candidate()` は、`claude_code` + `spawn_not_observed` を
bounded single retry の対象外としている。本文書はこの設計が AC2 の観測データと整合するかを評価する。

## 更新履歴（Issue #2161: native Codex CLI 撤去に伴う本評価の改訂）

本評価は元々、`_is_transient_infrastructure_candidate()` が `codex_cli` runtime に
限り `spawn_not_observed` を bounded single retry の対象とし、`claude_code` を
明示的に除外するという runtime 条件付き設計（当時 native Codex CLI がまだ
サポートされていた頃の実装形状、具体的には `route["runtime"] == "codex_cli"` を
条件とする分岐）を評価対象として書かれた。

Issue #2161 で native Codex CLI runtime route が撤去され、それに付随して
`codex_cli` 固有の `spawn_not_observed` bounded-retry 分岐も
`_is_transient_infrastructure_candidate()` から削除された。現行の実装は
runtime に依存せず、`spawn_not_observed` を常に bounded retry の対象外として
扱う（対象とするのは `agy_transient_quota_failure` /
`child_completed_but_artifact_not_materialized` という別の secondary-failure
種別のみであり、いずれも `spawn_not_observed` とは無関係）。

この runtime-independent 化は本評価が下した結論（`keep_excluded` を維持すべき）
を変更しない。評価対象だった `claude_code` + `spawn_not_observed` の扱いは
撤去前後で同一（対象外のまま）であり、以下の「観測データ」「評価」「結論」の
各節は改訂不要である。

## 機械可読な判定

```yaml
retry_policy_verdict: keep_excluded
current_design_consistent_with_observation: yes
control_spawn_not_observed_count: 11
control_trial_count: 15
production_spawn_not_observed_count: 6
production_trial_count: 15
hook_identity_available_when_tool_result_missing: 20
```

## 観測データ

- 有効 trial 総数: 30
- 失敗 trial 数: 24
- `spawn_not_observed` に分類された trial 数: 17
- wall-clock timeout した trial 数: 0
- `system/api_retry` が 1 件以上観測された trial 数: 0
- `tool_use_result.agentType` が欠落した trial のうち、hook channel には agent_type が存在した trial 数: 20 / 20

`spawn_not_observed` trial の diagnostic_cause 内訳:

| diagnostic_cause | count |
| --- | --- |
| `tool_result_identity_not_observed` | 17 |

## 評価

観測された `spawn_not_observed` はすべて `tool_result_identity_not_observed`、
すなわち `tool_use_result` に `agentType` フィールドが存在しないことに起因する。
該当 trial では Agent tool dispatch・SubagentStart hook・SubagentStop hook・
tool_result・terminal event がすべて観測されており、hook channel は正しい
`agent_type` を返し、`agentId` も両 channel で完全一致している。
wall-clock timeout も `system/api_retry` も 1 件も観測されていない。

これは infrastructure timing race ではなく、runtime が返す tool_use_result の
エンベロープ形状（同期完了型か `status: "async_launched"` 型か）に依存した、
決定論的な抽出経路の欠落である。同一条件で再実行すればエンベロープ形状の分岐に
応じて成功することがあるが、それは根本原因が解消されたからではない。

したがって「再実行したら通った」という事実だけを transient 判定の根拠にしてはならない。
bounded retry を `claude_code` + `spawn_not_observed` に適用すると、
識別 evidence が欠落したままの run をエンベロープ形状の当たり外れで
成功に見せかけることになり、genuine な identity/spawn failure を覆い隠す。

## 結論

現行設計（`claude_code` + `spawn_not_observed` を bounded retry 対象外とする）は
観測データと整合する。**維持すべきである**。

ただし現行コードのコメントが述べる理由（Claude の spawn evidence は
in-memory stdout にあるため miss は transient ではない）は、
本 research が観測した実際の機序（async 起動エンベロープに `agentType` が
含まれないこと）とは異なる。結論は正しいが根拠は更新されるべきである。

既存テスト `test_claude_spawn_not_observed_is_not_transient_candidate`
（`scripts/agent-ops/tests/test_agent_provider_route_smoke.py`）が固定している契約は
変更不要である。本 Issue では `_is_transient_infrastructure_candidate()` を変更しない。

silent retry、2 回以上の追加 retry、成功するまでの反復 retry はいずれも提案しない
（Issue #2013 Out of Scope により禁止されている）。

真に必要なのは retry ではなく、hook channel に実在する identity evidence を
抽出経路に取り込むことである。詳細は `conclusion.md` を参照。

