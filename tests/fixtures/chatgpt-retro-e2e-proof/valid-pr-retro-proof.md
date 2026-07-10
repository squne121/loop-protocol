<!-- RETRO_E2E_PROOF_V1 start -->
<!-- 以下は #1405 P0-5 の PR-target synthetic route proof を示す固定フィクスチャの本文である -->
```json
{
  "schema": "chatgpt_retro_execution_proof/v1",
  "proof_kind": "RETRO_E2E_PROOF_V1",
  "repo": "squne121/loop-protocol",
  "parent_issue": 1153,
  "target": {
    "kind": "pull_request",
    "number": 1153
  },
  "operation_index_ref": {
    "comment_url": "https://github.com/squne121/loop-protocol/pull/1153#issuecomment-4930000030",
    "payload_digest": "sha256:2222222222222222222222222222222222222222222222222222222222222222",
    "validation_verdict": "pass"
  },
  "chatgpt_context": {
    "marker_comment_url": "https://github.com/squne121/loop-protocol/pull/1153#issuecomment-4930000031",
    "marker_digest": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
    "resolve_live_status": "resolved",
    "resolve_live_output_digest": "sha256:3333333333333333333333333333333333333333333333333333333333333333",
    "local_file_access_used": false,
    "latitude_direct_access_used": false,
    "raw_trace_access_used": false,
    "github_connector_only": true,
    "proof_strength": {
      "context_resolvability": "machine_verified",
      "retrospective_result_schema": "machine_verified",
      "connector_only_execution": "declared_by_session_operator",
      "local_file_non_use": "declared_by_session_operator",
      "latitude_direct_non_use": "declared_by_session_operator",
      "machine_verifies_actual_chatgpt_tool_boundary": false
    },
    "resolver_evidence": {
      "command": "pnpm chatgpt-retro-context:resolve-live",
      "command_args_digest": "sha256:4444444444444444444444444444444444444444444444444444444444444444",
      "resolver_version_digest": "sha256:5555555555555555555555555555555555555555555555555555555555555555",
      "checked_at": "2026-07-09T12:20:00Z",
      "status": "resolved",
      "marker_comment_url": "https://github.com/squne121/loop-protocol/pull/1153#issuecomment-4930000031",
      "marker_digest": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
      "payload_digest": "sha256:2222222222222222222222222222222222222222222222222222222222222222",
      "evidence_ref_count": 2,
      "source_manifest_count": 3,
      "page_budget_exhausted": false,
      "reference_page_budget_exhausted": false,
      "resolved_comment_set_digest": "sha256:0ce6f11af5b4453298033c2a03f315834a84a5379b64827c159ed17849056a9f"
    }
  },
  "retrospective_result": {
    "schema": "chatgpt_retrospective_result/v1",
    "payload_digest": "sha256:0010519bac08dd3b89a2f986d082b663149d73726f5fa00dcc545d2c64b31ae6",
    "validation_verdict": "pass",
    "verdict": "approve"
  },
  "safety": {
    "raw_values_emitted": false,
    "forbidden_fields_scan": "pass",
    "prompt_excerpt_present": false,
    "tool_io_excerpt_present": false,
    "local_absolute_path_present": false,
    "credential_value_present": false,
    "free_form_instruction_trusted": false,
    "issue_or_pr_body_treated_as_untrusted_evidence": true
  },
  "evidence_mode": {
    "value": "synthetic_route_proof",
    "marker_prerequisite_evidence_mode": "synthetic_only",
    "real_runtime_capture_claimed": false,
    "real_pilot_verified_claimed": false,
    "allowed_real_pilot_upgrade": false,
    "cloud_pilot_claimed": false
  }
}
```
<!-- RETRO_E2E_PROOF_V1 end -->
<!-- 上記は固定フィクスチャの終端マーカーである -->

<!-- CHATGPT_RETROSPECTIVE_RESULT_V1 start -->
<!-- 以下は参照される chatgpt_retrospective_result/v1 の固定フィクスチャである -->
```json
{
  "schema": "chatgpt_retrospective_result/v1",
  "target": {
    "repo": "squne121/loop-protocol",
    "type": "pull_request",
    "number": 1153
  },
  "input_marker_digest": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
  "verdict": "approve",
  "findings": [
    {
      "severity": "low",
      "title": "PR route proof resolves cleanly via GitHub connector",
      "evidence_refs": [
        {
          "kind": "github_comment",
          "ref": "https://github.com/squne121/loop-protocol/pull/1153#issuecomment-4930000030",
          "digest": "sha256:2222222222222222222222222222222222222222222222222222222222222222"
        },
        {
          "kind": "github_comment",
          "ref": "https://github.com/squne121/loop-protocol/pull/1153#issuecomment-4930000031",
          "digest": "sha256:1111111111111111111111111111111111111111111111111111111111111111"
        }
      ],
      "claim": "The public GitHub PR comment chain (operation index, marker) resolves deterministically via the connector for this pull-request-target synthetic proof, and no local file access or provider-side direct access was used.",
      "recommendation": "No action required for this synthetic route proof target; live PR-target E2E capture remains deferred per Runtime Verification Applicability."
    }
  ],
  "follow_up_issue_candidates": [],
  "raw_values_emitted": false
}
```
<!-- CHATGPT_RETROSPECTIVE_RESULT_V1 end -->
<!-- 上記は固定フィクスチャの終端マーカーである -->
