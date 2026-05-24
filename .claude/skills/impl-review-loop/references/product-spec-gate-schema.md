# PRODUCT_SPEC_GATE_DECISION_V1 Schema

## Overview

`PRODUCT_SPEC_GATE_DECISION_V1` is the output of `evaluate_product_spec_gate.py`, a mutation-free routing gate that reads `CONTRACT_REVIEW_RESULT_V1.checks.product_spec_check` from the contract snapshot and emits a routing decision.

## Schema

```json
{
  "status": "ok",
  "applicability": "applicable | not_applicable | missing",
  "decision": "pass | fail | human_judgment | missing",
  "blocked_rule_ids": ["PS001", "PS002"],
  "contract_snapshot_url": "https://github.com/..../issues/333#issuecomment-...",
  "issue_url": "https://github.com/..../issues/333",
  "body_sha256": "abc123...",
  "routing_action": "continue | stop_human | refresh_contract_snapshot",
  "reason": "Optional explanation for anomalous paths"
}
```

## Field Semantics

### `status: "ok"`

**Authoritative signal**: `routing_action` field.

**NOT a success indicator**: `status: "ok"` is emitted on ALL return paths (including `refresh_contract_snapshot` and `stop_human`). It indicates the script ran without crashing, NOT that the gate decision was favorable.

Consumers must use `routing_action` as the authoritative success / failure / refresh signal. Never interpret `status: "ok"` alone as "gate passed."

### `routing_action`

Authoritative routing decision. Possible values:

- `continue`: Product spec gate passed; proceed to Step 1 (implementation).
- `stop_human`: Product spec check returned `fail` or `human_judgment`; escalate to human and do NOT start implementation.
- `refresh_contract_snapshot`: Contract snapshot is stale, invalid, or inconsistent; re-run `issue-contract-review` to refresh.

### `applicability` and `decision`

Normalized from `CONTRACT_REVIEW_RESULT_V1.checks.product_spec_check`.

- `applicability`: `applicable | not_applicable | missing`
- `decision`: `pass | fail | human_judgment | missing`

**Pair Invariant (Blocker 1)**:
- If `applicability == "not_applicable"` then `decision` MUST be `"pass"`.
- If `applicability == "not_applicable"` and `decision != "pass"`, the snapshot is inconsistent and must be refreshed.

### `blocked_rule_ids`

Normalized list of PS rule IDs (e.g., `["PS001", "PS002"]`) that caused the decision to be `fail` or `human_judgment`.

Extraction strategy (Blocker 1):
1. Try to read `product_spec_check.blocked_rule_ids` (direct list).
2. If absent, extract from `product_spec_check.blocked_reasons[].rule_id`.
3. If neither exists, return empty list.

### `contract_snapshot_url`

GitHub comment URL of the contract snapshot (e.g., `https://github.com/squne121/loop-protocol/issues/333#issuecomment-123456789`).

**Provenance (Blocker 2)**:
- Priority: CLI arg `--contract-snapshot-url` → snapshot field `.contract_snapshot_url` → `None`
- Do NOT fall back to `issue_url` (which is the Issue URL, not the snapshot comment URL).

### `issue_url`

GitHub Issue URL (e.g., `https://github.com/squne121/loop-protocol/issues/333`).

**Backward compatibility**: kept for reference, but `contract_snapshot_url` is authoritative for provenance.

### `body_sha256`

SHA256 hash of the contract snapshot comment body, extracted from the input JSON at the top-level (Blocker 3).

**Availability**: Preserved across all return paths (including missing-schema and refresh routes), NOT dropped on anomalous paths.

### `reason` (optional)

Human-readable explanation for anomalous paths:
- `"product_spec_check missing from contract snapshot"`
- `"Inconsistent product_spec_check: not_applicable requires decision=pass"`
- `"Invalid product_spec_check enum value"`

## Routing Table

| applicability | decision | blocked_reasons | → routing_action | Reason |
|---|---|---|---|---|
| applicable | pass | - | continue | Product spec passed |
| applicable | fail | PS001, PS002 | stop_human | Product spec failed; requires human decision |
| applicable | human_judgment | PS006 | stop_human | Ambiguous product spec; requires human decision |
| not_applicable | pass | - | continue | No product spec relevance; proceed |
| not_applicable | fail | PS001 | refresh_contract_snapshot | Inconsistent state (Blocker 1) |
| not_applicable | human_judgment | PS005 | refresh_contract_snapshot | Inconsistent state (Blocker 1) |
| (missing) | (missing) | - | refresh_contract_snapshot | product_spec_check absent; snapshot stale |
| invalid enum | invalid enum | - | refresh_contract_snapshot | Invalid enum value; snapshot invalid |

## Testing

Test fixtures in `.claude/skills/impl-review-loop/scripts/tests/fixtures/` cover:
- `pass.json`: pass (continue)
- `not_applicable.json`: not_applicable + pass (continue)
- `not_applicable_fail.json`: not_applicable + fail (refresh) — Blocker 1
- `not_applicable_human_judgment.json`: not_applicable + human_judgment (refresh) — Blocker 1
- `fail.json`: fail (stop_human)
- `human_judgment.json`: human_judgment (stop_human)
- `missing-schema.json`: missing product_spec_check (refresh)
- `missing-contract-root.json`: missing CONTRACT_REVIEW_RESULT_V1 root (refresh)
- `stale-snapshot.json`: invalid enum (refresh)

All tests validate:
1. Correct `routing_action`.
2. Correct enum values.
3. Preservation of `body_sha256` across all paths (Blocker 3).
4. Passthrough of `contract_snapshot_url` via CLI arg (Blocker 2).
5. Normalization of `blocked_rule_ids` from `blocked_reasons` (Blocker 1).
