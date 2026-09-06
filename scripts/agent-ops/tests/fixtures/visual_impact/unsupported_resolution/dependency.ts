// Issue #2525 fixture dependency, reachable from entry.ts only through the
// `@app/*` tsconfig path alias (see entry.ts) -- never through a relative
// specifier this walker's bare-import resolution understands. Used by the
// AC4 regression case: this file changes WITHOUT any of the fixture's
// config files (package.json / tsconfig.json / tsconfig.base.json /
// vite.config.ts / config/shared_resolve.ts) changing.
export function helper(): string {
  return 'dependency-value'
}
