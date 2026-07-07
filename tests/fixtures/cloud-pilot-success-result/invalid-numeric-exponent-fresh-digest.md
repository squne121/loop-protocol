# cloud_pilot_success_result/v1 フィクスチャ（負例）

この fixture は metrics.duration_seconds を指数表記(1.5e2)で記述した負例であり、JSON.parse による正規化後の digest が一致(fresh)していても raw JSON text の指数表記自体を checker が拒否することを確認するためのものである（OWNER Blocker 4, fix_delta iteration 2）。以下のマーカー行はチェッカーが参照する契約でありバイト単位で変更しない。
<!-- CLOUD_PILOT_SUCCESS_RESULT_V1 repo=squne121/loop-protocol target=issue:1153 parent_issue=1153 result_id=cloud-pilot-success-result-fixture-only-baseline -->

```json
{
  "schema": "cloud_pilot_success_result/v1",
  "result_id": "cloud-pilot-success-result-fixture-only-baseline",
  "parent_issue": "#1153",
  "contract_issue": "#1260",
  "placement_issue": "#1330",
  "target": {
    "kind": "issue",
    "number": 1153,
    "marker_value": "issue:1153"
  },
  "evidence_mode": "fixture_only",
  "decision_ready": false,
  "decision": "pending_fixture_only",
  "gate_refs": {
    "session_recording_smoke": {
      "issue": "#246",
      "state": "completed",
      "verdict": "pass",
      "evidence_digest": "sha256:cb09b61d585ab0c9adea12c3572beb0ab84e6686bdca258b9080be1f30aa1890"
    },
    "latitude_real_pilot_decision": {
      "issue": "#1220",
      "state": "completed",
      "decision": "approve_timeboxed_real_pilot",
      "decision_digest": "sha256:989176efd35ed64eedcc60fd5e6cab48b1f53812e5f5f2dad98a2d9846b123ae"
    },
    "latitude_distribution_gate": {
      "issue": "#1261",
      "state": "completed",
      "argv_exposure_state": "absent_verified",
      "remote_cleanup_state": "machine_verified"
    },
    "success_contract_checker": {
      "issue": "#1326",
      "state": "open",
      "checker_result": "unknown",
      "presented_as_real_target": false
    }
  },
  "metrics": {
    "duration_seconds": 1.5e2,
    "cost_usd": 0.42
  },
  "safety": {
    "redaction_status": "clean",
    "verdict": "pass",
    "blocked_reasons": []
  },
  "generated_at": "2026-07-06T12:00:00Z"
}
```

この digest 値は JSON.parse により 1.5e2 が 150 へ正規化された後の canonical 表現から正しく再計算された fresh digest である。raw JSON text の指数表記(1.5e2)自体を独立した scanner で検知して拒否することが本 fixture の意図である。
<!-- CLOUD_PILOT_SUCCESS_RESULT_DIGEST_V1 sha256=21e676c08a4f007122518ea97a2ccc419c1daaafc37de8436b45d1a0027449a0 -->
