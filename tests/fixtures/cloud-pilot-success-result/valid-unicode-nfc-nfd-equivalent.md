# cloud_pilot_success_result/v1 フィクスチャ（正例）

この fixture は Unicode NFC/NFD 表現の違いが正規化により同一 digest として受理される正例である。以下のマーカー行はチェッカーが参照する契約でありバイト単位で変更しない。
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
    "duration_seconds": 120,
    "cost_usd": 0.42
  },
  "safety": {
    "redaction_status": "clean",
    "verdict": "pass",
    "blocked_reasons": ["café verified"]
  },
  "generated_at": "2026-07-06T12:00:00Z"
}
```

この digest 値は fixture の内容から正しく算出された値である（diagnostic_context は fix_delta iteration 2 で closed object 化されたため、unicode 検証対象を safety.blocked_reasons に移動した）。
<!-- CLOUD_PILOT_SUCCESS_RESULT_DIGEST_V1 sha256=31be3dad07f2fb0cbc0825c5e120117b22f62f8f1f5e6aff93cb08605a44719a -->
