// Issue #2525 fixture producer entry. Imports `dependency.ts` through the
// `@app/*` path alias declared in tsconfig.base.json (inherited via this
// fixture's tsconfig.json `extends`) -- a bare specifier this module's
// walker treats as `external` (out of scope) and never follows, since it
// does not implement tsconfig `paths`/`baseUrl` resolution. That gap is
// exactly what `detectUnsupportedResolutionSettings()` reports as an
// unsupported-resolution diagnostic instead of silently under-counting
// `dependency.ts` as having no producer.
import { helper } from '@app/dependency'

export const marker = helper()
