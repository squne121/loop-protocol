// Issue #2525 fixture: `resolve` is assigned an externally-defined binding
// (imported from ./config/shared_resolve.ts) rather than an inline object
// literal. resolve_visual_impact.mjs's detectUnsupportedResolutionSettings()
// never executes this file (or the import it references) -- it can only
// see, from a text scan, that `resolve:` does not resolve to a literal
// object it can inspect directly.
import { defineConfig } from 'vite'
import { sharedResolveOptions } from './config/shared_resolve'

export default defineConfig({
  resolve: sharedResolveOptions,
})
