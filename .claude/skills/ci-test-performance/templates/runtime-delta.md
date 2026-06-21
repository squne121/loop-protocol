# Runtime Delta テンプレート

`ci-test-performance` Skill の runtime delta 記録テンプレート。
CI パフォーマンスの変化を記録するときに使用する。

## 使用方法

PR 本文または GitHub コメントにこのテンプレートを記録することで、CI 高速化の証跡を残す。

## テンプレート

```yaml
ci_runtime_delta_v1:
  pr_number: <int>
  issue_number: <int>
  measured_at: "<ISO8601>"
  baseline_source: ci_runtime_baseline_v1
  baseline_run_count: <int>             # baseline に使用した run 数（推奨: 20）

  before:
    job: "<job name>"
    p50_seconds: <float>
    p95_seconds: <float>
    run_ids:
      - "<GitHub Actions run ID>"

  after:
    job: "<job name>"
    p50_seconds: <float>
    p95_seconds: <float>
    run_ids:
      - "<GitHub Actions run ID>"

  delta:
    p50_delta_seconds: <float>          # 負の値 = 高速化
    p95_delta_seconds: <float>          # 負の値 = 高速化
    p50_improvement_pct: <float>        # 正の値 = 改善率 (%)
    p95_improvement_pct: <float>

  lanes_affected:
    fast_static: true | false
    python_unit: true | false
    contract_artifact: true | false
    integration: true | false

  decision:
    verdict: improved | regression | no_change | insufficient_data
    confidence: high | medium | low
    reason: "<判定の根拠>"
    # insufficient_data: run_count < 20 または p50/p95 が利用不可の場合
    # 1 回の実行だけで verdict を出さない
```

## 注意事項

- `baseline_run_count < 20` の場合は `confidence: low` / `verdict: insufficient_data` とする
- `before` と `after` の両方に複数の run ID を含める（少なくとも 3 runs 以上）
- `regression` の場合は PR を APPROVE してはならない（`reviewer_gate.approve_allowed: false`）
- bootstrap 3 runs は `confidence: low` として記録し、decision baseline 20 runs が揃うまで conclusive な判定をしない
