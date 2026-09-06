// Issue #2525 fixture: a Vite `resolve` config defined in a SEPARATE file
// and imported into vite.config.ts, rather than written inline. This is the
// "静的検査で安全に意味を確定できない Vite 設定" case (c) --
// resolve_visual_impact.mjs never imports/executes this file to discover
// that it configures `alias`; it only sees, from vite.config.ts's own text,
// that the `resolve` field references an externally-defined binding.
export const sharedResolveOptions = {
  alias: {
    '@shared': '../dependency.ts',
  },
}
