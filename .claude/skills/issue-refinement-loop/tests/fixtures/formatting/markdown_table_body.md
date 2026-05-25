# Test Issue: Markdown table body paths

## Outcome

Extract paths from markdown formatted tables.

| Component | Path | Status |
|-----------|------|--------|
| Skills | `.claude/skills/refinement` | Done |
| Source | `src/components/Button.tsx` | WIP |
| Tests | `tests/unit/button.test.ts` | TBD |

## In Scope

- Multi-path extraction from tables
- Format handling

## Acceptance Criteria

- AC1: Paths in tables are extracted
- AC2: Table markdown doesn't break extraction

## Verification Commands

```bash
uv run pytest tests/ -v
```
