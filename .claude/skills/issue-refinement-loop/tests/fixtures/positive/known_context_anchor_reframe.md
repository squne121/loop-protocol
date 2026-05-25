# Test Issue: Known context anchor reframe

## Outcome

Internal refactoring that was reviewed and reframed in context of existing scope.

## In Scope

- Internal module reorganization
- Existing paths remain in scope

## Acceptance Criteria

- AC1: Known context anchor reframe is applied
- AC2: Evidence sources include known_context

## Verification Commands

```bash
uv run pytest tests/ -v
```

Reference: Previously reviewed with scope confirmation that this doesn't expand beyond module boundaries.
