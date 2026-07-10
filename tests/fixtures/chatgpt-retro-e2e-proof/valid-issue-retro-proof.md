<!-- RETRO_E2E_PROOF_V1 start -->
<!-- 以下は #1405 の synthetic route proof を示す固定フィクスチャの本文である -->
```json
{
  "schema": "chatgpt_retro_execution_proof/v1",
  "proof_kind": "RETRO_E2E_PROOF_V1",
  "repo": "squne121/loop-protocol",
  "parent_issue": 1153,
  "target": {
    "kind": "issue",
    "number": 1405
  },
  "operation_index_ref": {
    "comment_url": "https://github.com/squne121/loop-protocol/issues/1405#issuecomment-4930000020",
    "payload_digest": "sha256:010529b4625782a08be002d613bb040d53c946352d6f1747861830f7253e5fda",
    "validation_verdict": "pass",
    "embedded_payload": {
      "schema": "agent_operation_session_index/v1",
      "repo": "squne121/loop-protocol",
      "parent_issue": 1153,
      "target": {
        "kind": "issue",
        "number": 1405
      },
      "operation": {
        "kind": "issue_comment",
        "github_event_ref": {
          "kind": "github_comment",
          "ref": "https://github.com/squne121/loop-protocol/issues/1405#issuecomment-4925899031",
          "digest": "sha256:92cc7aad88072ae88fb68db5c65737499cdc81bcab81ce52a6f1967e54f1a894"
        },
        "occurred_at": "2026-07-09T12:00:00Z"
      },
      "agent_run": {
        "run_id": "run-1405-issue-001",
        "agent_surface": "claude_code",
        "evidence_mode": "synthetic_route_proof",
        "capability_verdict": "supported",
        "raw_values_emitted": false
      },
      "public_artifacts": {
        "run_report_comment_url": "https://github.com/squne121/loop-protocol/issues/1405#issuecomment-4930000001",
        "run_report_payload_digest": "sha256:86c882e91dada55e290cde2709982bb6f15634c267371f4a811b32bba369872a",
        "retro_index_comment_url": "https://github.com/squne121/loop-protocol/issues/1405#issuecomment-4930000002",
        "retro_index_payload_digest": "sha256:e8c38c28ac42ba9182acb70f5dbca2a986c9ae1e6728c9e00c4356e55968554c",
        "retro_index_source_set_digest": "sha256:de599f10cbcfebdc5ad057bb685d0be0beef0ad031652b91565be8079862e567",
        "chatgpt_marker_comment_url": "https://github.com/squne121/loop-protocol/issues/1405#issuecomment-4930000003",
        "chatgpt_marker_digest": "sha256:be5c56de28e189a046c9847367c006574260050d22a91f002a35f450597d574e"
      },
      "verification": {
        "resolver_command": "pnpm chatgpt-retro-context:resolve-live",
        "resolver_status": "resolved",
        "checked_at": "2026-07-09T12:05:00Z",
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
    "marker_comment_url": "https://github.com/squne121/loop-protocol/issues/1405#issuecomment-4930000003",
    "marker_digest": "sha256:be5c56de28e189a046c9847367c006574260050d22a91f002a35f450597d574e",
    "resolve_live_status": "resolved",
    "resolve_live_output_digest": "sha256:a2cc8ef64dc305456c175c930ba0dec8182a2d4933e89e89c330076df484591b",
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
      "command_args_digest": "sha256:39e84be88fbf51e41810c5a7cd8f69423d98fdd0bcda4af073e74dc3b31a96d3",
      "resolver_version_digest": "sha256:865ea9ff75e97958a54574089f0d96290ac37c589474060db4f2bf42164cfa00",
      "checked_at": "2026-07-09T12:10:00Z",
      "status": "resolved",
      "marker_comment_url": "https://github.com/squne121/loop-protocol/issues/1405#issuecomment-4930000003",
      "marker_digest": "sha256:be5c56de28e189a046c9847367c006574260050d22a91f002a35f450597d574e",
      "payload_digest": "sha256:b5d874a80b0f7db5d9fce2601eb53b5d78587cd1997ac96b6a5318dc537e6d10",
      "evidence_ref_count": 2,
      "source_manifest_count": 3,
      "page_budget_exhausted": false,
      "reference_page_budget_exhausted": false,
      "resolved_comment_set_digest": "sha256:70415ac4082e54cca0457515ce2eda0c03a1af34e00cedc832f54fb25b7300c9"
    }
  },
  "retrospective_result": {
    "schema": "chatgpt_retrospective_result/v1",
    "payload_digest": "sha256:c25387074d8b0b7634c451c5af36bd8088643d7d7b363727ee6e9c8df4b620c3",
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
    "type": "issue",
    "number": 1405
  },
  "input_marker_digest": "sha256:be5c56de28e189a046c9847367c006574260050d22a91f002a35f450597d574e",
  "verdict": "approve",
  "findings": [
    {
      "severity": "low",
      "title": "Route proof resolves cleanly via GitHub connector",
      "evidence_refs": [
        {
          "kind": "github_comment",
          "ref": "https://github.com/squne121/loop-protocol/issues/1405#issuecomment-4930000020",
          "digest": "sha256:010529b4625782a08be002d613bb040d53c946352d6f1747861830f7253e5fda"
        },
        {
          "kind": "github_comment",
          "ref": "https://github.com/squne121/loop-protocol/issues/1405#issuecomment-4930000003",
          "digest": "sha256:be5c56de28e189a046c9847367c006574260050d22a91f002a35f450597d574e"
        }
      ],
      "claim": "The public GitHub comment chain (operation index, run report, retro index, marker) resolves deterministically via the connector, and no local file access or provider-side direct access was used to build this synthetic proof.",
      "recommendation": "No action required for this synthetic route proof target; provider-side pilot adoption remains a separate pending governance decision (#1220, #1261)."
    }
  ],
  "follow_up_issue_candidates": [],
  "raw_values_emitted": false
}
```
<!-- CHATGPT_RETROSPECTIVE_RESULT_V1 end -->
<!-- 上記は固定フィクスチャの終端マーカーである -->
