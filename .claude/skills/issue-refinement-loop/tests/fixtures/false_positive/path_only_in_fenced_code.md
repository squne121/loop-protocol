# Test Issue: Path extraction from fenced code

## Outcome

Need to implement path extraction, but don't extract paths from code examples.

## In Scope

- Documentation updates

## Acceptance Criteria

- AC1: Fenced code paths are excluded
- AC2: Regular text paths are included

## Verification Commands

```bash
# Example of a path that should NOT be extracted in fenced code
# These are just examples in code blocks
uv run pytest .claude/skills/test_runner/tests/ -v
```

Code examples are not extracted paths.
