// Unrelated placeholder producer kept in the head registry so the head-side
// schema validation still satisfies producers.modules.minItems=1 even
// though the ORIGINAL mapping (entry.ts) was removed (Issue #2019 AC5:
// removing a producer mapping must not silently look like an empty diff).
export const unrelatedFixtureMarker = 'unrelated'
