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
    "payload_digest": "sha256:246cb54162c0164206347ac33ddf89da3692c041635df43a3356cb918d603eb8",
    "validation_verdict": "pass",
    "embedded_payload": {
      "schema": "agent_operation_session_index/v1",
      "repo": "squne121/loop-protocol",
      "parent_issue": 1153,
      "target": {
        "kind": "pull_request",
        "number": 1299
      },
      "operation": {
        "kind": "pr_comment",
        "github_event_ref": {
          "kind": "github_comment",
          "ref": "https://github.com/squne121/loop-protocol/pull/1299#issuecomment-4930000010",
          "digest": "sha256:be38f0f5c6a85f16f863bb699b2de17fd9f29ff55a7b78562270733521692b15"
        },
        "occurred_at": "2026-07-02T09:30:00Z"
      },
      "agent_run": {
        "run_id": "run-1405-pr-001",
        "agent_surface": "claude_code",
        "evidence_mode": "synthetic_route_proof",
        "capability_verdict": "supported",
        "raw_values_emitted": false
      },
      "public_artifacts": {
        "run_report_comment_url": "https://github.com/squne121/loop-protocol/pull/1299#issuecomment-4930000011",
        "run_report_payload_digest": "sha256:d1c9dcb2a5a5731bd1d00f35fb52f0e65f099eded93be7e0373315d45af7540e",
        "retro_index_comment_url": "https://github.com/squne121/loop-protocol/pull/1299#issuecomment-4930000012",
        "retro_index_payload_digest": "sha256:6a8f5f21a9e0735007d06726c3023408949ae924e5f2e8359c3d5956468e7485",
        "retro_index_source_set_digest": "sha256:62c67b28f53662ef4765d33a50319a9fc18cfe3de7e4aed896fa4c314ccafd93",
        "chatgpt_marker_comment_url": "https://github.com/squne121/loop-protocol/pull/1299#issuecomment-4930000013",
        "chatgpt_marker_digest": "sha256:6d422b49d8d840de070b79ecb0933cae0cf1966059f1f722fdd66463fc108cd1"
      },
      "verification": {
        "resolver_command": "pnpm chatgpt-retro-context:resolve-live",
        "resolver_status": "resolved",
        "checked_at": "2026-07-02T09:35:00Z",
        "checker_version": "check-agent-operation-session-index.mjs@1405",
        "public_safe": true,
        "semantic_checks": [
          "target.kind_url_alignment",
          "public_artifacts.number_alignment",
          "event_kind_mapping",
          "raw_values_emitted_false"
        ]
      }
    }
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
    "payload_digest": "sha256:f1f62c4437380bf08f35553fb36ecf397eeffaccc78da0f0869b9c37ee0beefe",
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
          "digest": "sha256:246cb54162c0164206347ac33ddf89da3692c041635df43a3356cb918d603eb8"
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
