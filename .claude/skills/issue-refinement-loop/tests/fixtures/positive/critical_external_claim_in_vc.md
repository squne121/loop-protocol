# Test Issue: Critical external claims in VC

## Outcome

Need to verify official CLI API behavior and authentication flow against published documentation.

## In Scope

- Analysis of current official API specification
- Validation of auth migration behavior

## Acceptance Criteria

- AC1: External claims are detected
- AC2: Web research policy is triggered appropriately

## Verification Commands

The following command requires external verification:
```bash
# Verify against official API documentation: https://api.example.com/docs
uv run pytest .claude/skills/issue-refinement-loop/tests/ -v
```

Additional requirement: Verify current CLI authentication flow matches the official spec documentation.
