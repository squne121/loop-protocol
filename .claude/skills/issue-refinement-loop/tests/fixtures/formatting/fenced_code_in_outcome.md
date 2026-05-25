# Test Issue: Fenced code in outcome

## Outcome

Implementation requires updates to actual files, excluding examples:

Real files:
- `src/real/file.ts`
- `src/utils/Real.ts`

Code examples (should be excluded):
```typescript
// Example paths that should NOT be extracted:
src/example/NotReal.ts
import { helper } from 'src/example/NotReal';
```

The actual implementation files are listed above without code fencing.

## In Scope

- Real source file updates
- Exclude example code paths

## Acceptance Criteria

- AC1: Fenced code paths excluded
- AC2: Real paths included

## Verification Commands

```bash
uv run pytest tests/ -v
```
