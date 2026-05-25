# Test Issue: Anchor reframe with scope signal exclusion

## Outcome

Reviewed in #123456 with scope reframe: scope is not expanding beyond the explicitly designed boundaries even though internal structure changes.

## In Scope

- Changes to `.claude/skills` layer (refactoring)
- Updates to `docs/product` layer (coordination)
- API surface remains stable

## Acceptance Criteria

- AC1: Scope signals are correctly excluded by anchor reframe
- AC2: Both manual and API changes are in scope

## Verification Commands

```bash
uv run pytest .claude/skills/issue-refinement-loop/tests/ -v
```
