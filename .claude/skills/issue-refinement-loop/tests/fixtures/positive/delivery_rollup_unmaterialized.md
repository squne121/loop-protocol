# Test Issue: Extract unmaterialized child slots

## Outcome

This is a parent tracker Issue with delivery rollup tracking.

## In Scope

- #100 (未起票) - Initial planning document
- #101 (unmaterialized) - Implementation phase  
- #102 (TBD) - Verification and testing
- Code change tracking with (#103) (未起票) first sub-issue

## Acceptance Criteria

- AC1: Unmaterialized slots are correctly extracted
- AC2: Multiple marker types are supported

## Verification Commands

```bash
uv run pytest .claude/skills/issue-refinement-loop/tests/ -v
```
